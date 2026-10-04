"""Controller-owned persistence: Project State, factual operation ledger, and legacy audit data."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import HYPOTHESIS_STATUS_ENUM, SOURCE_ENUM, STATE_SCHEMA_VERSION
from .checkpoint import prepare_checkpoint, validate_checkpoint
from .core import AgentError, ProviderTurn, _normalized, _now, _task_hash, _trim
from .project_state import (apply_project_changes, complete_review, empty_project_state, initialize_project_state, validate_handoff)

class StateStore:
    """Controller-owned state. The model proposes patches; Python applies them atomically."""

    def __init__(self, state_dir: Path, task: str, *, reset: bool = False, cold_restart: bool = False) -> None:
        self.state_dir = state_dir.expanduser().resolve()
        existed = self.state_dir.exists()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.state_dir / "state.json"
        self.log_path = self.state_dir / "state.log"
        self.pending_path = self.state_dir / "pending_reasoning.jsonl"
        self.pending_iterations_path = self.state_dir / "pending_iterations.jsonl"
        self.failed_assimilation_path = self.state_dir / "failed_assimilation.jsonl"
        self.context_archive_path = self.state_dir / "context_archive.jsonl"
        self.reasoning_archive_path = self.state_dir / "reasoning_archive.jsonl"
        if reset:
            for path in (
                self.state_path,
                self.log_path,
                self.pending_path,
                self.pending_iterations_path,
                self.failed_assimilation_path,
                self.context_archive_path,
                self.reasoning_archive_path,
            ):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
        if cold_restart and existed and not reset:
            try:
                state = self._read_state()
                checkpoint = validate_checkpoint(state, task)
            except (AgentError, TypeError, ValueError, KeyError, UnicodeError) as exc:
                raise AgentError(f"RECOVERY FAILED: {self.state_path}: {exc}") from exc
            # Working mutations since the committed boundary are not a recovery point.
            # Keep the factual ledger/counter, so OP IDs remain unique after restart.
            restored = copy.deepcopy(state)
            restored["project_state"] = copy.deepcopy(checkpoint["project_state"])
            restored["checkpoint_handoff"] = checkpoint["handoff"]
            restored["run"] = {"status": "running", "last_finish_summary": ""}
            if restored != state:
                restored["version"] += 1
                restored["metadata"]["updated_at"] = _now()
                self._write_atomic(restored)
            return
        if self.state_path.exists():
            state = self.load()
            existing_task = state.get("task", {}).get("original")
            if existing_task != task:
                raise AgentError(
                    f"persistent state belongs to a different task: {self.state_path}. "
                    "Use --reset-state or choose another --state-dir."
                )
        else:
            state = self._new_state(task)
            self._write_atomic(state)
            self._append_log({"kind": "state_created", "version": 0})

    @staticmethod
    def _new_state(task: str) -> dict[str, Any]:
        now = _now()
        return {
            "schema_version": STATE_SCHEMA_VERSION,
            "version": 0,
            "task": {
                "original": task,
                "sha256": _task_hash(task),
                "immutable": True,
            },
            "current_focus": "",
            "verified": [],
            "hypotheses": [],
            "unresolved": [],
            "inspected": [],
            "changes": [],
            "failures": [],
            "loop_signals": [],
            # Small controller-owned record of Worker decisions to replace raw working material
            # in future prompts. Exact originals live in context_archive.jsonl; the factual
            # operation ledger remains untouched.
            "context_dispositions": [],
            # Controller-owned append-only factual ledger of executed shell operations.
            # The LLM never patches this field directly.
            "operations": [],
            "counters": {
                "verified": 0,
                "hypothesis": 0,
                "unresolved": 0,
                "inspected": 0,
                "change": 0,
                "failure": 0,
                "loop": 0,
                "operation": 0,
                "context_disposition": 0,
            },
            "project_state": empty_project_state(),
            "run": {
                "status": "running",
                "last_finish_summary": "",
            },
            "metadata": {
                "created_at": now,
                "updated_at": now,
                # Highest factual OP already consolidated into semantic state for Worker prompting.
                # The complete append-only operations ledger remains on disk.
                "worker_ops_compacted_through": 0,
            },
        }

    def _read_state(self) -> dict[str, Any]:
        def object_fields(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError(f"duplicate JSON field: {key}")
                result[key] = value
            return result
        def invalid_number(value):
            raise ValueError(f"invalid JSON number: {value}")
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"),
                               object_pairs_hook=object_fields, parse_constant=invalid_number)
        except (OSError, ValueError, UnicodeError) as exc:
            raise AgentError(f"cannot read persistent state {self.state_path}: {exc}") from exc
        if not isinstance(state, dict):
            raise AgentError(f"unsupported or invalid state file: {self.state_path}")
        return state

    def load(self) -> dict[str, Any]:
        state = self._read_state()
        # Backward-compatible one-time migration from the first persistent-state format.
        if state.get("schema_version") == 1 and STATE_SCHEMA_VERSION == 2:
            state["schema_version"] = 2
            state.setdefault("operations", [])
            state.setdefault("counters", {}).setdefault("operation", 0)
            self._write_atomic(state)
            self._append_log({"kind": "state_migrated", "from_schema": 1, "to_schema": 2})
        if state.get("schema_version") != STATE_SCHEMA_VERSION:
            raise AgentError(f"unsupported or invalid state file: {self.state_path}")
        # Optional controller-owned fields added within schema v2 remain backward compatible.
        changed = False
        if "context_dispositions" not in state:
            state["context_dispositions"] = []
            changed = True
        counters = state.setdefault("counters", {})
        if "context_disposition" not in counters:
            counters["context_disposition"] = 0
            changed = True
        if "project_state" not in state:
            state["project_state"] = empty_project_state()
            changed = True
        if changed:
            self._write_atomic(state)
        return state

    def _write_atomic(self, state: dict[str, Any]) -> None:
        tmp = self.state_dir / f".state.json.tmp.{os.getpid()}"
        data = json.dumps(state, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.state_path)
        # Persist the directory entry after publishing the complete file image.
        # If this fails, publication may already be visible; never claim rollback.
        directory_fd = os.open(self.state_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _append_log(self, payload: dict[str, Any]) -> None:
        event = {"at": _now(), **payload}
        with open(self.log_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    def record_manager_failure(self, error: str) -> None:
        self._append_log({"kind": "state_manager_failure", "error": error})

    def record_state_trigger(self, reason: str, turn: ProviderTurn) -> None:
        self._append_log({
            "kind": "state_cycle_trigger",
            "reason": reason,
            "worker_usage": {
                "prompt_tokens": turn.prompt_tokens,
                "completion_tokens": turn.completion_tokens,
                "reasoning_tokens": turn.reasoning_tokens,
                "finish_reason": turn.finish_reason,
            },
        })

    def record_context_drop(
        self,
        *,
        handles: list[str],
        intent: str,
        dropped_items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Persist the Worker's context-disposal intent without deleting factual evidence.

        This is controller-owned state: the Worker chooses among removable handles, while Core
        records the decision and keeps the operation ledger/state history intact.
        """
        state = self.load()
        state.setdefault("context_dispositions", [])
        state.setdefault("counters", {}).setdefault("context_disposition", 0)
        state["counters"]["context_disposition"] += 1
        disposition_id = f"D{state['counters']['context_disposition']:04d}"
        record = {
            "id": disposition_id,
            "at": _now(),
            "handles": list(handles),
            "op_ids": [
                str(item.get("op_id", ""))
                for item in dropped_items
                if str(item.get("op_id", ""))
            ],
            "intent": intent.strip(),
            "approx_tokens_removed": sum(
                int(item.get("approx_tokens", 0) or 0) for item in dropped_items
            ),
            "chars_removed": sum(int(item.get("chars", 0) or 0) for item in dropped_items),
        }
        state["context_dispositions"].append(record)
        # Keep the Worker-facing state bounded. The append-only state.log below retains every event.
        state["context_dispositions"] = state["context_dispositions"][-64:]
        state["version"] = int(state.get("version", 0)) + 1
        state["metadata"]["updated_at"] = _now()
        self._write_atomic(state)
        self._append_log({
            "kind": "context_dropped",
            "version": state["version"],
            "disposition": record,
        })
        return record

    def record_context_replacement(
        self,
        *,
        mode: str,
        replacements: list[dict[str, Any]],
        note: str,
    ) -> dict[str, Any]:
        """Persist one in-place WorkingContext replacement decision plus exact raw evidence.

        The bounded ``state.json`` record contains only metadata.  Exact original/replacement
        messages are written to an append-only controller archive that is never projected into the
        Worker prompt.  This keeps chronology visible through the replacement itself without
        duplicating large raw payloads in semantic state.
        """
        if mode not in {"tombstone", "compacted"}:
            raise AgentError(f"unsupported context replacement mode: {mode}")
        if not replacements:
            raise AgentError("context replacement requires at least one item")

        state = self.load()
        state.setdefault("context_dispositions", [])
        state.setdefault("counters", {}).setdefault("context_disposition", 0)
        state["counters"]["context_disposition"] += 1
        prefix = "D" if mode == "tombstone" else "C"
        disposition_id = f"{prefix}{state['counters']['context_disposition']:04d}"

        handles = [str(item.get("handle", "")) for item in replacements]
        op_ids = [str(item.get("op_id", "")) for item in replacements if str(item.get("op_id", ""))]
        before_tokens = sum(int(item.get("original_approx_tokens", 0) or 0) for item in replacements)
        after_tokens = sum(int(item.get("replacement_approx_tokens", 0) or 0) for item in replacements)
        before_chars = sum(int(item.get("original_chars", 0) or 0) for item in replacements)
        after_chars = sum(int(item.get("replacement_chars", 0) or 0) for item in replacements)
        record = {
            "id": disposition_id,
            "at": _now(),
            "mode": mode,
            "handles": handles,
            "op_ids": op_ids,
            "note": note.strip(),
            "approx_tokens_before": before_tokens,
            "approx_tokens_after": after_tokens,
            "approx_tokens_freed": before_tokens - after_tokens,
            "chars_before": before_chars,
            "chars_after": after_chars,
            "chars_freed": before_chars - after_chars,
        }

        # Archive exact evidence first.  If this write fails, the caller has not yet mutated the
        # in-memory WorkingContext, so replacement cannot silently destroy the only full raw copy.
        archive_event = {
            "at": _now(),
            "kind": "context_replacement_archive",
            "disposition_id": disposition_id,
            "mode": mode,
            "items": [
                {
                    "handle": str(item.get("handle", "")),
                    "kind": str(item.get("kind", "")),
                    "op_id": str(item.get("op_id", "")),
                    "step": int(item.get("step", 0) or 0),
                    "label": str(item.get("label", "")),
                    "note": str(item.get("note", "")),
                    "original_message": copy.deepcopy(item.get("original_message", {})),
                    "replacement_message": copy.deepcopy(item.get("replacement_message", {})),
                    "original_approx_tokens": int(item.get("original_approx_tokens", 0) or 0),
                    "replacement_approx_tokens": int(item.get("replacement_approx_tokens", 0) or 0),
                }
                for item in replacements
            ],
        }
        with open(self.context_archive_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(archive_event, ensure_ascii=False) + "\n")

        state["context_dispositions"].append(record)
        state["context_dispositions"] = state["context_dispositions"][-64:]
        state["version"] = int(state.get("version", 0)) + 1
        state["metadata"]["updated_at"] = _now()
        self._write_atomic(state)
        self._append_log({
            "kind": "context_replaced",
            "version": state["version"],
            "disposition": record,
        })
        return record

    def get_project_state(self) -> dict[str, Any]:
        return copy.deepcopy(self.load().get("project_state", empty_project_state()))

    def initialize_project(self, data: dict[str, Any], *, step: int) -> dict[str, Any]:
        state = self.load()
        operation_count = int(state.get("counters", {}).get("operation", 0) or 0)
        project = initialize_project_state(state.get("project_state", empty_project_state()), data, operation_count=operation_count)
        updated = copy.deepcopy(state)
        updated["project_state"] = project
        updated["checkpoint_handoff"] = ""
        updated["version"] = int(state.get("version", 0)) + 1
        updated.setdefault("metadata", {})["updated_at"] = _now()
        prepare_checkpoint(updated, kind="initial", step=step, handoff="")
        self._write_atomic(updated)
        self._append_log({"kind": "project_initialized", "version": updated["version"], "step": step, "project_revision": project["revision"]})
        return copy.deepcopy(project)

    def update_project(self, changes: list[dict[str, Any]], *, step: int) -> dict[str, Any]:
        state = self.load()
        project = apply_project_changes(state.get("project_state", empty_project_state()), changes)
        updated = copy.deepcopy(state)
        updated["project_state"] = project
        updated["version"] = int(state.get("version", 0)) + 1
        updated.setdefault("metadata", {})["updated_at"] = _now()
        self._write_atomic(updated)
        self._append_log({"kind": "project_updated", "version": updated["version"], "step": step, "project_revision": project["revision"], "changes": copy.deepcopy(changes)})
        return copy.deepcopy(project)

    def acknowledge_periodic_review(self, *, step: int) -> dict[str, Any]:
        state = self.load()
        operation_count = int(state.get("counters", {}).get("operation", 0) or 0)
        project = copy.deepcopy(state.get("project_state", empty_project_state()))
        if not project.get("initialized"):
            raise AgentError("Project State is not initialized")
        project["last_review_operation"] = operation_count
        updated = copy.deepcopy(state)
        updated["project_state"] = project
        updated["version"] = int(state.get("version", 0)) + 1
        updated.setdefault("metadata", {})["updated_at"] = _now()
        self._write_atomic(updated)
        self._append_log({"kind": "periodic_review_acknowledged", "version": updated["version"], "step": step, "operation_count": operation_count})
        return copy.deepcopy(project)

    def complete_project_review(self, *, step: int, note: str = "", handoff: str = "") -> dict[str, Any]:
        state = self.load()
        operation_count = int(state.get("counters", {}).get("operation", 0) or 0)
        handoff = validate_handoff(handoff)
        project = complete_review(state.get("project_state", empty_project_state()), operation_count=operation_count)
        updated = copy.deepcopy(state)
        # Publish State and the single latest handoff in the same atomic replacement.
        updated["checkpoint_handoff"] = handoff
        updated["project_state"] = project
        updated["version"] = int(state.get("version", 0)) + 1
        updated.setdefault("metadata", {})["updated_at"] = _now()
        prepare_checkpoint(updated, kind="high", step=step, handoff=handoff)
        self._write_atomic(updated)
        self._append_log({"kind": "project_review_completed", "version": updated["version"], "step": step, "project_revision": project["revision"], "note": note})
        return copy.deepcopy(project)

    def archive_reasoning_reset(self, items: list[dict[str, Any]], *, step: int, reason: str) -> None:
        if not items:
            return
        event = {"at": _now(), "kind": "reasoning_discarded", "step": step, "reason": reason, "items": copy.deepcopy(items)}
        with open(self.reasoning_archive_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        self._append_log({"kind": "reasoning_discarded", "step": step, "reason": reason, "items": len(items)})

    def record_operation(self, result: dict[str, Any]) -> dict[str, Any]:
        """Append one controller-observed shell operation atomically.

        This ledger is factual controller memory, not LLM-generated state.  It records
        what was actually requested/executed and a small result excerpt so old work
        remains visible without retaining full stdout forever.
        """
        state = self.load()
        state.setdefault("operations", [])
        state.setdefault("counters", {}).setdefault("operation", 0)
        state["counters"]["operation"] += 1
        op_id = f"OP{state['counters']['operation']:04d}"

        stdout = str(result.get("stdout", ""))
        stderr = str(result.get("stderr", ""))
        launch_error = str(result.get("launch_error", ""))
        execution_error = str(result.get("execution_error", ""))
        error = str(result.get("error", ""))
        output_notice = str(result.get("output_notice", ""))
        excerpt_parts = []
        if stdout.strip():
            excerpt_parts.append("stdout: " + _trim(stdout.strip(), 1200))
        if stderr.strip():
            excerpt_parts.append("stderr: " + _trim(stderr.strip(), 800))
        if launch_error.strip():
            excerpt_parts.append("launch_error: " + launch_error.strip())
        if execution_error.strip():
            excerpt_parts.append("execution_error: " + execution_error.strip())
        if error.strip():
            excerpt_parts.append("error: " + error.strip())
        if output_notice.strip():
            excerpt_parts.append(output_notice.strip())

        record = {
            "id": op_id,
            "at": _now(),
            "command": str(result.get("command", "")).strip(),
            "network": bool(result.get("network", False)),
            "exit_code": result.get("exit_code"),
            "timed_out": bool(result.get("timed_out", False)),
            "duration": result.get("duration"),
            "result_excerpt": "\n".join(excerpt_parts),
            "output_withheld": bool(result.get("output_withheld", False)),
            "output_chars": result.get("output_chars"),
            "output_limit_chars": result.get("output_limit_chars"),
        }
        state["operations"].append(record)
        state["version"] = int(state.get("version", 0)) + 1
        state["metadata"]["updated_at"] = _now()
        self._write_atomic(state)
        self._append_log({"kind": "operation_recorded", "version": state["version"], "operation": record})
        return record

    @staticmethod
    def _pending_reasoning_id(event: dict[str, Any]) -> str:
        existing = event.get("id")
        if isinstance(existing, str) and existing:
            return existing
        material = "\0".join(
            [
                str(event.get("at", "")),
                str(event.get("finish_reason", "")),
                str(event.get("reasoning", "")),
            ]
        )
        return "R" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]

    def add_pending_reasoning(self, reasoning: str, finish_reason: str | None) -> dict[str, Any]:
        event = {
            "at": _now(),
            "finish_reason": finish_reason,
            "reasoning": reasoning,
        }
        event["id"] = self._pending_reasoning_id(event)
        with open(self.pending_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        return event

    def load_pending_reasoning(self) -> list[dict[str, Any]]:
        if not self.pending_path.exists():
            return []
        items: list[dict[str, Any]] = []
        try:
            for line in self.pending_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                value = json.loads(line)
                if isinstance(value, dict):
                    value.setdefault("id", self._pending_reasoning_id(value))
                    items.append(value)
        except (OSError, json.JSONDecodeError) as exc:
            raise AgentError(f"cannot read pending reasoning {self.pending_path}: {exc}") from exc
        return items

    def remove_pending_reasoning(self, ids: set[str]) -> None:
        if not ids or not self.pending_path.exists():
            return
        items = self.load_pending_reasoning()
        kept = [item for item in items if item.get("id") not in ids]
        if not kept:
            try:
                self.pending_path.unlink()
            except FileNotFoundError:
                pass
            return
        tmp = self.state_dir / f".pending_reasoning.jsonl.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as handle:
            for item in kept:
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.pending_path)

    def clear_pending_reasoning(self) -> None:
        """Compatibility helper used by tests/tools that intentionally clear all pending reasoning."""
        try:
            self.pending_path.unlink()
        except FileNotFoundError:
            pass

    def enqueue_iteration(
        self,
        *,
        kind: str,
        transcript: list[dict[str, str]],
        worker_reasoning: str = "",
        op_id: str | None = None,
        reasoning_id: str | None = None,
    ) -> dict[str, Any]:
        if op_id:
            iteration_id = "IT-" + op_id
        elif reasoning_id:
            iteration_id = "IT-" + reasoning_id
        else:
            material = json.dumps(
                {"kind": kind, "transcript": transcript, "worker_reasoning": worker_reasoning},
                ensure_ascii=False, sort_keys=True,
            )
            iteration_id = "IT-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]
        record = {
            "id": iteration_id,
            "at": _now(),
            "kind": kind,
            "op_id": op_id,
            "reasoning_id": reasoning_id,
            "transcript": transcript,
            "worker_reasoning": worker_reasoning,
        }
        with open(self.pending_iterations_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._append_log({"kind": "worker_iteration_queued", "iteration": iteration_id, "op_id": op_id})
        return record

    def load_pending_iterations(self) -> list[dict[str, Any]]:
        if not self.pending_iterations_path.exists():
            return []
        items: list[dict[str, Any]] = []
        try:
            for line in self.pending_iterations_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                value = json.loads(line)
                if isinstance(value, dict) and isinstance(value.get("id"), str):
                    items.append(value)
        except (OSError, json.JSONDecodeError) as exc:
            raise AgentError(f"cannot read pending iterations {self.pending_iterations_path}: {exc}") from exc
        return items

    def remove_pending_iterations(self, ids: set[str]) -> None:
        if not ids or not self.pending_iterations_path.exists():
            return
        items = self.load_pending_iterations()
        kept = [item for item in items if item.get("id") not in ids]
        if not kept:
            try:
                self.pending_iterations_path.unlink()
            except FileNotFoundError:
                pass
            return
        tmp = self.state_dir / f".pending_iterations.jsonl.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as handle:
            for item in kept:
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.pending_iterations_path)

    @staticmethod
    def _processed_operation_number(iteration_ids: list[str]) -> int | None:
        numbers: list[int] = []
        for iteration_id in iteration_ids:
            if not iteration_id.startswith("IT-OP"):
                continue
            suffix = iteration_id[5:]
            if suffix.isdigit():
                numbers.append(int(suffix))
        return max(numbers) if numbers else None

    def commit_state_cycle(
        self,
        *,
        base_state: dict[str, Any],
        provisional_state: dict[str, Any],
        processed_iterations: list[str],
        applied_ids: list[str],
        quarantined_iterations: list[str] | None = None,
        processed_reasoning_ids: list[str] | None = None,
        persist: bool = True,
    ) -> dict[str, Any]:
        """Publish one whole State Manager cycle atomically.

        Individual Manager calls mutate only an in-memory provisional state.  The accepted prefix is
        written once at cycle end.  Failed/attractor units are quarantined from automatic replay but
        remain preserved in failed_assimilation.jsonl and factual operations remain in state.json.
        With persist=False, return/audit the same accepted continuation and consume
        the same pending units, without replacing durable semantic-history state.
        """
        current = self.load()
        if persist and current.get("version") != base_state.get("version"):
            raise AgentError("state changed during state cycle; refusing stale publication")
        quarantined_iterations = list(quarantined_iterations or [])
        processed_reasoning_ids = list(processed_reasoning_ids or [])
        published = copy.deepcopy(provisional_state)
        published.setdefault("metadata", {})
        previous_compacted = int(published["metadata"].get("worker_ops_compacted_through", 0) or 0)
        processed_op = self._processed_operation_number(processed_iterations)
        if processed_op is not None:
            published["metadata"]["worker_ops_compacted_through"] = max(previous_compacted, processed_op)

        changed = published != base_state
        if changed:
            published["version"] = int(base_state.get("version", 0)) + 1
            published["metadata"]["updated_at"] = _now()
            if persist:
                self._write_atomic(published)
        if not persist:
            # Same accepted output and audit trail, but no durable semantic-history publication.
            self._append_log({"kind": "reasoning_continuation", "state": published})
        self.remove_pending_iterations(set(processed_iterations) | set(quarantined_iterations))
        self.remove_pending_reasoning(set(processed_reasoning_ids))
        self._append_log({
            "kind": "state_cycle_committed",
            "from_version": base_state.get("version", 0),
            "to_version": published.get("version", base_state.get("version", 0)),
            "processed_iterations": processed_iterations,
            "quarantined_iterations": quarantined_iterations,
            "applied": applied_ids,
        })
        return published

    def persist_reasoning_state(self, reasoning_state: dict[str, Any]) -> dict[str, Any]:
        """Durably publish accepted cognitive memory without reviving legacy semantic history."""
        current = self.load()
        fields = ("current_focus", "hypotheses", "unresolved", "failures", "loop_signals")
        memory = {
            field: copy.deepcopy(reasoning_state.get(field, "" if field == "current_focus" else []))
            for field in fields
        }
        if current.get("reasoning_memory") == memory:
            return current
        updated = copy.deepcopy(current)
        updated["reasoning_memory"] = memory
        updated["version"] = int(current.get("version", 0)) + 1
        updated.setdefault("metadata", {})["updated_at"] = _now()
        self._write_atomic(updated)
        self._append_log({
            "kind": "reasoning_state_persisted",
            "from_version": current.get("version", 0),
            "to_version": updated["version"],
        })
        return updated

    def record_failed_assimilation(
        self,
        *,
        transcript: list[dict[str, str]],
        pending_reasoning: list[dict[str, Any]],
        error: str,
        iteration_id: str | None = None,
    ) -> None:
        """Persist one failed bounded assimilation unit without scheduling an automatic retry."""
        event = {
            "at": _now(),
            "iteration_id": iteration_id,
            "error": error,
            "transcript": transcript,
            "pending_reasoning": pending_reasoning,
        }
        with open(self.failed_assimilation_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    def _next_id(self, state: dict[str, Any], counter: str, prefix: str) -> str:
        state["counters"][counter] += 1
        return f"{prefix}{state['counters'][counter]:04d}"

    @staticmethod
    def _find_by_id(items: list[dict[str, Any]], item_id: str) -> dict[str, Any] | None:
        return next((item for item in items if item.get("id") == item_id), None)

    @staticmethod
    def _has_text(items: list[dict[str, Any]], field: str, value: str) -> bool:
        needle = _normalized(value)
        return any(_normalized(str(item.get(field, ""))) == needle for item in items)

    def apply_patch(
        self,
        patch: dict[str, Any],
        *,
        base_state: dict[str, Any] | None = None,
        persist: bool = True,
        bump_version: bool = True,
    ) -> tuple[dict[str, Any], list[str]]:
        """Apply a validated patch. Invalid references reject the whole patch.

        Global state cycles pass an explicit provisional base with persist=False/bump_version=False;
        the complete accepted prefix is published once by commit_state_cycle().
        """
        state = self.load() if base_state is None else copy.deepcopy(base_state)
        new_state = copy.deepcopy(state)
        applied: list[str] = []

        focus = patch.get("focus_update", {})
        if not isinstance(focus, dict) or not isinstance(focus.get("set"), bool) or not isinstance(focus.get("value"), str):
            raise AgentError("invalid state patch focus_update")
        if focus["set"]:
            new_state["current_focus"] = focus["value"].strip()
            applied.append("focus")

        def add_records(patch_key: str, state_key: str, field: str, counter: str, prefix: str) -> None:
            values = patch.get(patch_key)
            if not isinstance(values, list):
                raise AgentError(f"invalid state patch field {patch_key}")
            for item in values:
                if not isinstance(item, dict):
                    raise AgentError(f"invalid item in {patch_key}")
                value = item.get(field)
                basis = item.get("basis")
                source = item.get("source")
                if not isinstance(value, str) or not value.strip() or not isinstance(basis, str) or source not in SOURCE_ENUM:
                    raise AgentError(f"invalid item in {patch_key}")
                if patch_key == "verified_add" and source != "transcript":
                    raise AgentError("verified_add requires transcript evidence; reasoning alone is not verification")
                if self._has_text(new_state[state_key], field, value):
                    continue
                record = {
                    "id": self._next_id(new_state, counter, prefix),
                    field: value.strip(),
                    "basis": basis.strip(),
                    "source": source,
                    "created_at": _now(),
                }
                new_state[state_key].append(record)
                applied.append(record["id"])

        add_records("verified_add", "verified", "text", "verified", "V")
        add_records("unresolved_add", "unresolved", "text", "unresolved", "Q")
        add_records("inspected_add", "inspected", "path", "inspected", "I")
        add_records("changes_add", "changes", "text", "change", "C")
        add_records("failures_add", "failures", "text", "failure", "F")
        add_records("loop_signals_add", "loop_signals", "text", "loop", "L")

        # Status fields are added after generic unresolved records are created.
        for item in new_state["unresolved"]:
            item.setdefault("status", "open")
            item.setdefault("resolution", "")

        hypotheses_add = patch.get("hypotheses_add")
        if not isinstance(hypotheses_add, list):
            raise AgentError("invalid state patch field hypotheses_add")
        for item in hypotheses_add:
            if not isinstance(item, dict):
                raise AgentError("invalid item in hypotheses_add")
            text = item.get("text")
            status = item.get("status")
            basis = item.get("basis")
            source = item.get("source")
            if (
                not isinstance(text, str) or not text.strip()
                or status not in HYPOTHESIS_STATUS_ENUM
                or not isinstance(basis, str)
                or source not in SOURCE_ENUM
            ):
                raise AgentError("invalid item in hypotheses_add")
            if self._has_text(new_state["hypotheses"], "text", text):
                continue
            record = {
                "id": self._next_id(new_state, "hypothesis", "H"),
                "text": text.strip(),
                "status": status,
                "basis": basis.strip(),
                "source": source,
                "created_at": _now(),
                "updated_at": _now(),
            }
            new_state["hypotheses"].append(record)
            applied.append(record["id"])

        updates = patch.get("hypotheses_update")
        if not isinstance(updates, list):
            raise AgentError("invalid state patch field hypotheses_update")
        for update in updates:
            if not isinstance(update, dict):
                raise AgentError("invalid item in hypotheses_update")
            item_id = update.get("id")
            status = update.get("status")
            basis = update.get("basis")
            if not isinstance(item_id, str) or status not in HYPOTHESIS_STATUS_ENUM or not isinstance(basis, str):
                raise AgentError("invalid item in hypotheses_update")
            record = self._find_by_id(new_state["hypotheses"], item_id)
            if record is None:
                raise AgentError(f"state patch references unknown hypothesis {item_id}")
            record["status"] = status
            record["basis"] = basis.strip()
            record["updated_at"] = _now()
            applied.append(item_id)

        resolves = patch.get("unresolved_resolve")
        if not isinstance(resolves, list):
            raise AgentError("invalid state patch field unresolved_resolve")
        for update in resolves:
            if not isinstance(update, dict):
                raise AgentError("invalid item in unresolved_resolve")
            item_id = update.get("id")
            resolution = update.get("resolution")
            basis = update.get("basis")
            if not isinstance(item_id, str) or not isinstance(resolution, str) or not isinstance(basis, str):
                raise AgentError("invalid item in unresolved_resolve")
            record = self._find_by_id(new_state["unresolved"], item_id)
            if record is None:
                raise AgentError(f"state patch references unknown unresolved item {item_id}")
            record["status"] = "resolved"
            record["resolution"] = resolution.strip()
            record["basis"] = basis.strip()
            record["updated_at"] = _now()
            applied.append(item_id)

        if applied:
            if bump_version:
                new_state["version"] = int(state.get("version", 0)) + 1
                new_state["metadata"]["updated_at"] = _now()
            if persist:
                self._write_atomic(new_state)
                self._append_log({
                    "kind": "patch_applied",
                    "version": new_state["version"],
                    "applied": applied,
                    "patch": patch,
                })
            return new_state, applied

        if persist:
            self._append_log({
                "kind": "patch_noop",
                "version": state.get("version", 0),
                "patch": patch,
            })
        return state, []

    def mark_finished(self, summary: str) -> None:
        state = self.load()
        state["run"]["status"] = "finished"
        state["run"]["last_finish_summary"] = summary
        state["metadata"]["updated_at"] = _now()
        state["version"] = int(state.get("version", 0)) + 1
        self._write_atomic(state)
        self._append_log({"kind": "finished", "version": state["version"], "summary": summary})
