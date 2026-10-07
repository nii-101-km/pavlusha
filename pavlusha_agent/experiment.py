"""Benchmark-only telemetry for context-discipline experiments.

This module is observational except for the optional visible context meter. It does not
participate in semantic state, choose actions, or alter shell results.
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

    def record_context_preflight(self, *, step: int, phase: str, breakdown: dict[str, Any]) -> None:
        self._append({"kind": "context_preflight", "step": step, "phase": phase, **breakdown})

    def record_worker_turn(
        self, *, step: int, turn: ProviderTurn, meter_mode: str,
        context_budget: int, compaction_threshold: int | None,
        context_control: str = "off", context_stats: dict[str, int] | None = None,
    ) -> None:
        event = {
            "kind": "worker_turn",
            "step": step,
            "meter_mode": meter_mode,
            "context_control": context_control,
            "context_budget": context_budget,
            "compaction_threshold": compaction_threshold,
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
        elif kind == "drop_context":
            event["items"] = [
                {"id": str(item.get("id", "")), "reason": str(item.get("reason", ""))}
                for item in data.get("items", [])
                if isinstance(item, dict)
            ]
            # Preserve the legacy normalized fields when old callers use ids+intent.
            event["ids"] = list(data.get("ids", []))
            if data.get("intent"):
                event["intent"] = str(data.get("intent", ""))
        elif kind == "compact_context":
            event["items"] = [
                {"id": str(item.get("id", "")), "summary": str(item.get("summary", ""))}
                for item in data.get("items", [])
                if isinstance(item, dict)
            ]
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

    def record_context_notice(
        self, *, step: int, level: str, prompt_tokens: int | None, managed_tokens: int,
        managed_budget: int, removable_items: int, removable_approx_tokens: int, guidance_mode: str,
    ) -> None:
        self._append({
            "kind": "context_notice",
            "step": step,
            "level": level,
            "guidance_mode": guidance_mode,
            "prompt_tokens_hint": prompt_tokens,
            "managed_tokens": managed_tokens,
            "managed_budget": managed_budget,
            "managed_ratio": managed_tokens / managed_budget,
            "removable_items": removable_items,
            "removable_approx_tokens": removable_approx_tokens,
        })


    def record_maintenance_gate(
        self, *, step: int, event: str, prompt_tokens: int | None, managed_tokens: int,
        managed_budget: int, enter_ratio: float, release_ratio: float, removable_items: int,
        removable_approx_tokens: int, action: str = "",
    ) -> None:
        self._append({
            "kind": "maintenance_gate",
            "step": step,
            "event": event,
            "prompt_tokens_hint": prompt_tokens,
            "managed_tokens": managed_tokens,
            "managed_budget": managed_budget,
            "managed_ratio": managed_tokens / managed_budget,
            "enter_ratio": enter_ratio,
            "release_ratio": release_ratio,
            "removable_items": removable_items,
            "removable_approx_tokens": removable_approx_tokens,
            "action": action,
        })

    def record_context_drop(
        self, *, step: int, disposition_id: str, dropped_items: list[dict[str, Any]], intent: str
    ) -> None:
        self._append({
            "kind": "context_drop_applied",
            "step": step,
            "disposition_id": disposition_id,
            "intent": intent,
            "ids": [str(item.get("handle", "")) for item in dropped_items],
            "op_ids": [str(item.get("op_id", "")) for item in dropped_items if item.get("op_id")],
            "approx_tokens_removed": sum(int(item.get("approx_tokens", 0) or 0) for item in dropped_items),
            "chars_removed": sum(int(item.get("chars", 0) or 0) for item in dropped_items),
        })

    def record_context_replacement(
        self, *, step: int, disposition: dict[str, Any], replacements: list[dict[str, Any]]
    ) -> None:
        self._append({
            "kind": "context_replacement_applied",
            "step": step,
            "disposition_id": str(disposition.get("id", "")),
            "mode": str(disposition.get("mode", "")),
            "ids": [str(item.get("handle", "")) for item in replacements],
            "op_ids": [str(item.get("op_id", "")) for item in replacements if item.get("op_id")],
            "approx_tokens_before": int(disposition.get("approx_tokens_before", 0) or 0),
            "approx_tokens_after": int(disposition.get("approx_tokens_after", 0) or 0),
            "approx_tokens_freed": int(disposition.get("approx_tokens_freed", 0) or 0),
        })

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


def context_meter_message(
    *, previous_prompt_tokens: int, context_budget: int, compaction_threshold: int
) -> dict[str, str]:
    """Neutral resource telemetry; deliberately contains no instruction to conserve context."""
    threshold_pct = 100.0 * previous_prompt_tokens / compaction_threshold
    context_pct = 100.0 * previous_prompt_tokens / context_budget
    return {
        "role": "user",
        "content": (
            "CONTEXT METER (telemetry only): previous Worker request used "
            f"{previous_prompt_tokens} prompt tokens. Semantic compaction threshold: "
            f"{compaction_threshold} tokens ({threshold_pct:.1f}% of threshold). "
            f"Provider context capacity: {context_budget} tokens ({context_pct:.1f}% used)."
        ),
    }
