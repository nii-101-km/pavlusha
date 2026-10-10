"""Controller-owned persistence: Project State and the factual operation ledger."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

from .config import STATE_SCHEMA_VERSION
from .checkpoint import prepare_checkpoint, validate_checkpoint
from .core import AgentError, _now, _task_hash, _trim
from .project_state import (apply_project_changes, complete_review, empty_project_state, initialize_project_state, validate_handoff)

class StateStore:
    """Atomically persist Project State, checkpoints, and executed operation records."""

    def __init__(self, state_dir: Path, task: str, *, reset: bool = False, cold_restart: bool = False) -> None:
        self.state_dir = state_dir.expanduser().resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        has_existing_state = any(self.state_dir.iterdir())
        self.state_path = self.state_dir / "state.json"
        self.log_path = self.state_dir / "state.log"
        if reset:
            for path in (self.state_path, self.log_path):
                path.unlink(missing_ok=True)
        if cold_restart and has_existing_state and not reset:
            try:
                state = self._read_state()
                checkpoint = validate_checkpoint(state, task)
            except (AgentError, TypeError, ValueError, KeyError, UnicodeError) as exc:
                raise AgentError(f"RECOVERY FAILED: {self.state_path}: {exc}") from exc
            # Working mutations since the committed boundary are not a recovery point.
            # Keep the factual ledger/counter, so OP IDs remain unique after restart.
            restored = copy.deepcopy(state)
            restored["project_state"] = copy.deepcopy(checkpoint["project_state"])
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
            "operations": [],
            "counters": {"operation": 0},
            "project_state": empty_project_state(),
            "run": {
                "status": "running",
                "last_finish_summary": "",
            },
            "metadata": {
                "created_at": now,
                "updated_at": now,
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
        if type(state.get("schema_version")) is not int or state["schema_version"] != STATE_SCHEMA_VERSION:
            raise AgentError(f"unsupported or invalid state file: {self.state_path}")
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

    def get_project_state(self) -> dict[str, Any]:
        return copy.deepcopy(self.load()["project_state"])

    def initialize_project(self, data: dict[str, Any], *, step: int) -> dict[str, Any]:
        state = self.load()
        operation_count = int(state.get("counters", {}).get("operation", 0) or 0)
        project = initialize_project_state(state["project_state"], data, operation_count=operation_count)
        updated = copy.deepcopy(state)
        updated["project_state"] = project
        updated["version"] = int(state.get("version", 0)) + 1
        updated.setdefault("metadata", {})["updated_at"] = _now()
        prepare_checkpoint(updated, kind="initial", step=step, handoff="")
        self._write_atomic(updated)
        self._append_log({"kind": "project_initialized", "version": updated["version"], "step": step, "project_revision": project["revision"]})
        return copy.deepcopy(project)

    def update_project(self, changes: list[dict[str, Any]], *, step: int) -> dict[str, Any]:
        state = self.load()
        project = apply_project_changes(state["project_state"], changes)
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
        project = copy.deepcopy(state["project_state"])
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
        project = complete_review(state["project_state"], operation_count=operation_count)
        updated = copy.deepcopy(state)
        # Publish State and handoff together in the committed checkpoint envelope.
        updated["project_state"] = project
        updated["version"] = int(state.get("version", 0)) + 1
        updated.setdefault("metadata", {})["updated_at"] = _now()
        prepare_checkpoint(updated, kind="high", step=step, handoff=handoff)
        self._write_atomic(updated)
        self._append_log({"kind": "project_review_completed", "version": updated["version"], "step": step, "project_revision": project["revision"], "note": note})
        return copy.deepcopy(project)

    def record_operation(self, result: dict[str, Any]) -> dict[str, Any]:
        """Append one controller-observed shell operation atomically.

        This ledger is factual controller memory, not LLM-generated state.  It records
        what was actually requested/executed and a small result excerpt so old work
        remains visible without retaining full stdout forever.
        """
        state = self.load()
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
            "duration_seconds": result.get("duration_seconds"),
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

    def mark_finished(self, summary: str) -> None:
        state = self.load()
        state["run"]["status"] = "finished"
        state["run"]["last_finish_summary"] = summary
        state["metadata"]["updated_at"] = _now()
        state["version"] = int(state.get("version", 0)) + 1
        self._write_atomic(state)
        self._append_log({"kind": "finished", "version": state["version"], "summary": summary})
