"""Observational telemetry for Worker turns, checkpoints, and runtime recovery.

This module records runtime observations without choosing actions or altering results.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .core import ProviderTurn, _now


class ExperimentRecorder:
    def __init__(self, state_dir: Path, *, reset: bool = False) -> None:
        self.path = state_dir.expanduser().resolve() / "experiment.jsonl"
        if reset:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass

    def _append(self, payload: dict[str, Any]) -> None:
        event = {"at": _now(), **payload}
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    def record_context_recovery(self, *, step: int, checkpoint: str, error: str) -> None:
        self._append({"kind": "context_overflow_recovery", "step": step,
                      "checkpoint_sha256": checkpoint, "error": error})

    def record_human_escalation(self, *, step: int, attempts: int, resumed: bool) -> None:
        self._append({"kind": "human_escalation", "reason": "reasoning_loop_exhaustion",
                      "step": step, "recovery_attempts": attempts, "resumed": resumed})

    def record_expert_call(self, *, step: int, event: dict[str, Any]) -> None:
        """Factual request metadata only; no prompt, answer, key or reasoning."""
        self._append({"kind": "expert_call", "step": step, **event})

    def record_operation_loop(self, *, step: int, cycle_length: int, steps: tuple[int, ...]) -> None:
        self._append({"kind": "operation_loop", "step": step, "cycle_length": cycle_length,
                      "repetitions": 3, "matched_steps": list(steps)})

    def record_generation_interrupted(self, *, step: int, turn: ProviderTurn) -> None:
        self._append({"kind": "generation_interrupted", "reason": "user_cancel", "step": step,
                      "interrupted_reasoning": turn.reasoning_content,
                      "interrupted_content": turn.content, "interrupted_tool_calls": turn.tool_calls})

    def record_context_trim(self, *, step: int, steps: list[int]) -> None:
        self._append({"kind": "context_trim", "step": step, "removed_steps": steps,
                      "removed_turns": len(steps), "project_state_rollback": False, "files_rollback": False})

    def record_context_preflight(self, *, step: int, phase: str, breakdown: dict[str, Any]) -> None:
        self._append({"kind": "context_preflight", "step": step, "phase": phase, **breakdown})

    def record_worker_turn(
        self, *, step: int, turn: ProviderTurn, context_budget: int,
        context_stats: dict[str, int],
    ) -> None:
        event = {
            "kind": "worker_turn",
            "step": step,
            "context_budget": context_budget,
            "prompt_tokens": turn.prompt_tokens,
            "completion_tokens": turn.completion_tokens,
            "reasoning_tokens": turn.reasoning_tokens,
            "finish_reason": turn.finish_reason,
        }
        if context_stats:
            event.update({f"working_{key}": value for key, value in context_stats.items()})
        self._append(event)

    def record_reasoning_loop(
        self, *, step: int, mode: str, signal: dict[str, Any], recovery_attempt: int,
        interrupted: bool, turn: ProviderTurn | None = None, exhausted: bool = False,
    ) -> None:
        event = {
            "kind": "reasoning_loop", "step": step, "mode": mode, **signal,
            "recovery_attempt": recovery_attempt, "interrupted": interrupted,
            "exhausted": exhausted,
            "reasoning_tokens": turn.reasoning_tokens if turn is not None else None,
        }
        if interrupted and turn is not None:
            event["interrupted_reasoning"] = turn.reasoning_content
            event["interrupted_content"] = turn.content
            event["interrupted_tool_calls"] = turn.tool_calls
        self._append(event)

    def record_worker_action(self, *, step: int, kind: str, data: dict[str, Any]) -> None:
        event: dict[str, Any] = {"kind": "worker_action", "step": step, "action": kind}
        if kind == "shell":
            event["command"] = str(data.get("command", ""))
            event["network"] = bool(data.get("network", False))
        elif kind == "project_init":
            event["design_items"] = len(data.get("design", []))
            event["work_items"] = len(data.get("work", []))
        elif kind == "project_update":
            event["changes"] = [str(item.get("op", "")) for item in data.get("changes", [])]
        elif kind == "project_review_complete":
            event["note"] = str(data.get("note", ""))
        elif kind == "finish":
            event["summary"] = str(data.get("summary", ""))
        self._append(event)

    def record_prefix_changed(self, *, step: int, reason: str) -> None:
        self._append({"kind": "historical_prompt_changed", "step": step, "reason": reason})

    def record_operation_telemetry(
        self, *, step: int, op_id: str, result: dict[str, Any]
    ) -> None:
        self._append({
            "kind": "operation_telemetry",
            "step": step,
            "op_id": op_id,
            "command": str(result.get("command", "")),
            "stdout_chars": len(str(result.get("stdout", ""))),
            "stderr_chars": len(str(result.get("stderr", ""))),
        })
