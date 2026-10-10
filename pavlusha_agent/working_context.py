"""Chronological Worker history retained intact until a committed checkpoint."""
from __future__ import annotations

import copy
from dataclasses import dataclass


@dataclass
class _Entry:
    message: dict[str, str]
    kind: str
    op_id: str
    step: int


class WorkingContext:
    """Keep reasoning, actions, and results together in their original order."""

    def __init__(self) -> None:
        self._entries: list[_Entry] = []
        self.current_step = 0

    def __len__(self) -> int:
        return len(self._entries)

    def messages(self) -> list[dict[str, str]]:
        return [entry.message for entry in self._entries]

    def step_count(self) -> int:
        """Count retained Worker steps, including rejected and recovery attempts."""
        return len({entry.step for entry in self._entries})

    def step_records(self) -> list[dict[str, object]]:
        """Return exact chronological records for the external history archive."""
        return [
            {"step": entry.step, "message": copy.deepcopy(entry.message),
             "kind": entry.kind, "op_id": entry.op_id}
            for entry in self._entries
        ]

    def append(
        self, message: dict[str, str], *, kind: str = "message", op_id: str = "", step: int = 0,
    ) -> None:
        self._entries.append(_Entry(message, kind, op_id, step or self.current_step))

    def clear(self) -> None:
        self._entries.clear()

    def completed_turn_steps(self) -> list[int]:
        """An assistant action followed by its factual response completes a retained turn."""
        actions, completed = set(), []
        for entry in self._entries:
            if entry.step <= 0 or entry.kind in {'user_message', 'context_trim'}:
                continue
            if entry.message.get('role') == 'assistant':
                actions.add(entry.step)
            elif entry.message.get('role') == 'user' and entry.step in actions and entry.step not in completed:
                completed.append(entry.step)
        return completed

    def last_turn_records(self, count: int) -> list[dict[str, object]]:
        steps = self.completed_turn_steps()
        if type(count) is not int or count < 1 or count > len(steps):
            raise ValueError(f"Choose 1–{len(steps)} available completed turns.")
        selected = set(steps[-count:])
        return [record for record in self.step_records()
                if record['step'] in selected and record['kind'] not in {'user_message', 'context_trim'}]

    def trim_last_turns(self, count: int) -> None:
        selected = {record['step'] for record in self.last_turn_records(count)}
        self._entries = [entry for entry in self._entries
                         if entry.step not in selected or entry.kind in {'user_message', 'context_trim'}]

    def telemetry(self) -> dict[str, int]:
        return {
            "active_messages": len(self._entries),
            "steps": self.step_count(),
            "total_chars": sum(len(str(entry.message.get("role", ""))) +
                               len(str(entry.message.get("content", "")))
                               for entry in self._entries),
        }
