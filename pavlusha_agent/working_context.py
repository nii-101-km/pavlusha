"""Worker-owned ephemeral context handles with controller-enforced retention rules.

System/task/state messages are reconstructed outside this container on every turn and therefore
cannot be targeted by Worker context actions.  Raw removable entries may be replaced *in place*
with a short tombstone or compact summary.  The chronological slot survives; only the heavy raw
payload leaves future Worker prompts.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from math import ceil

from .core import AgentError


@dataclass(frozen=True)
class ContextItem:
    handle: str
    kind: str
    op_id: str
    step: int
    approx_tokens: int
    chars: int
    label: str
    max_tombstone_reason_chars: int = 0
    max_summary_chars: int = 0


@dataclass(frozen=True)
class ContextReplacement:
    """One planned/applied in-place replacement of a raw working-context entry."""

    handle: str
    mode: str
    kind: str
    op_id: str
    step: int
    label: str
    note: str
    original_message: dict[str, str]
    replacement_message: dict[str, str]
    original_approx_tokens: int
    original_chars: int
    replacement_approx_tokens: int
    replacement_chars: int

    @property
    def approx_tokens_freed(self) -> int:
        return self.original_approx_tokens - self.replacement_approx_tokens

    @property
    def chars_freed(self) -> int:
        return self.original_chars - self.replacement_chars


@dataclass
class _Entry:
    message: dict[str, str]
    handle: str | None = None
    kind: str = "message"
    op_id: str = ""
    step: int = 0
    approx_tokens: int = 0
    chars: int = 0
    label: str = ""
    state: str = "raw"


class WorkingContext:
    """Controller-owned Worker transcript with replaceable raw observation slots.

    Only entries created with ``removable=True`` receive a W-handle.  While an entry is ``raw``
    that handle is a cleanup capability.  After replacement the same W-handle remains embedded in
    the chronological tombstone/summary text, but is no longer an active capability and therefore
    cannot be replaced again.  Immutable prompt layers and controller state never live here.
    """

    def __init__(self) -> None:
        self._entries: list[_Entry] = []
        self._next_handle = 0
        self.current_step = 0

    @staticmethod
    def _estimate_tokens(message: dict[str, str]) -> tuple[int, int]:
        # Deliberately dependency-free telemetry. This is only a per-item size estimate; provider
        # prompt_tokens remains authoritative for the whole request.
        chars = len(str(message.get("role", ""))) + len(str(message.get("content", "")))
        return max(1, ceil(chars / 4)), chars

    def __len__(self) -> int:
        return len(self._entries)

    def messages(self) -> list[dict[str, str]]:
        return [entry.message for entry in self._entries]

    def step_count(self) -> int:
        """Number of retained Worker steps, including rejected/recovery attempts."""
        return len({entry.step for entry in self._entries})

    def step_records(self) -> list[dict[str, object]]:
        """Exact chronological records for the existing external history archive."""
        return [{"step": entry.step, "handle": entry.handle, "message": copy.deepcopy(entry.message),
                 "kind": entry.kind, "op_id": entry.op_id, "state": entry.state}
                for entry in self._entries]

    @staticmethod
    def _replacement_prefix(entry: _Entry, mode: str) -> str:
        op = f" {entry.op_id}" if entry.op_id else ""
        if mode == "tombstone":
            return f"[CONTEXT TOMBSTONE {entry.handle}{op}] "
        if mode == "compacted":
            return f"[CONTEXT COMPACTED {entry.handle}{op}] "
        raise ValueError(f"unknown replacement mode: {mode}")

    @classmethod
    def _replacement_note_limit_chars(cls, entry: _Entry, *, mode: str) -> int:
        """Largest note that still reduces our token estimate.

        There is deliberately no arbitrary global character cap.  The only size invariant is
        resource based: the rendered replacement must be smaller than the raw item it replaces,
        using the same dependency-free chars/4 estimate as managed-context accounting.
        """
        if entry.handle is None or entry.approx_tokens <= 1:
            return 0
        prefix = cls._replacement_prefix(entry, mode)
        # To force ceil(after_chars / 4) < before_tokens, after_chars must be at most
        # 4 * (before_tokens - 1).  ``_estimate_tokens`` also counts the role string.
        fixed_chars = len(str(entry.message.get("role", ""))) + len(prefix)
        return max(0, 4 * (entry.approx_tokens - 1) - fixed_chars)

    @classmethod
    def _tombstone_limit_chars(cls, entry: _Entry) -> int:
        return cls._replacement_note_limit_chars(entry, mode="tombstone")

    @classmethod
    def _summary_limit_chars(cls, entry: _Entry) -> int:
        return cls._replacement_note_limit_chars(entry, mode="compacted")

    @classmethod
    def _is_replaceable_entry(cls, entry: _Entry) -> bool:
        return (
            entry.handle is not None
            and entry.state == "raw"
            and (cls._tombstone_limit_chars(entry) > 0 or cls._summary_limit_chars(entry) > 0)
        )

    def append(
        self,
        message: dict[str, str],
        *,
        removable: bool = False,
        kind: str = "message",
        op_id: str = "",
        step: int = 0,
        label: str = "",
    ) -> str | None:
        if step == 0:
            step = self.current_step
        approx_tokens, chars = self._estimate_tokens(message)
        handle: str | None = None
        if removable:
            candidate = f"W{self._next_handle + 1:04d}"
            probe = _Entry(
                message=message,
                handle=candidate,
                kind=kind,
                op_id=op_id,
                step=step,
                approx_tokens=approx_tokens,
                chars=chars,
                label=label.strip(),
            )
            # Do not issue a capability for a raw item whose shortest legal replacement cannot
            # reduce the prompt. Such tiny material is protected instead of being billable to the
            # maintenance gate with no useful cleanup action.
            if self._is_replaceable_entry(probe):
                self._next_handle += 1
                handle = candidate
        self._entries.append(_Entry(
            message=message,
            handle=handle,
            kind=kind,
            op_id=op_id,
            step=step,
            approx_tokens=approx_tokens,
            chars=chars,
            label=label.strip(),
        ))
        return handle

    def retain_tail_messages(self, keep: int) -> None:
        if keep <= 0:
            self._entries.clear()
            return
        self._entries = self._entries[-keep:]

    def removable_items(self) -> list[ContextItem]:
        return [
            ContextItem(
                handle=entry.handle,
                kind=entry.kind,
                op_id=entry.op_id,
                step=entry.step,
                approx_tokens=entry.approx_tokens,
                chars=entry.chars,
                label=entry.label,
                max_tombstone_reason_chars=self._tombstone_limit_chars(entry),
                max_summary_chars=self._summary_limit_chars(entry),
            )
            for entry in self._entries
            if self._is_replaceable_entry(entry)
        ]

    def _raw_entries(self) -> dict[str, _Entry]:
        return {
            str(entry.handle): entry
            for entry in self._entries
            if self._is_replaceable_entry(entry)
        }

    def _validate_handles(self, handles: list[str], *, action: str) -> dict[str, _Entry]:
        if not handles:
            raise AgentError(f"{action} requires at least one removable context handle")
        if len(set(handles)) != len(handles):
            raise AgentError(f"{action} handles must be unique")

        active = self._raw_entries()
        unknown = [handle for handle in handles if handle not in active]
        if unknown:
            raise AgentError(
                f"{action} references unavailable or non-removable handle(s): "
                + ", ".join(unknown)
            )
        return active

    def _make_replacement(
        self, entry: _Entry, *, mode: str, note: str,
    ) -> ContextReplacement:
        prefix = self._replacement_prefix(entry, mode)
        replacement_message = {
            "role": str(entry.message.get("role", "user")),
            "content": prefix + note.strip(),
        }
        replacement_tokens, replacement_chars = self._estimate_tokens(replacement_message)
        if replacement_tokens >= entry.approx_tokens:
            action = "compact_context" if mode == "compacted" else "drop_context"
            raise AgentError(
                f"{action} replacement for {entry.handle} does not reduce the raw item "
                f"(~{entry.approx_tokens} -> ~{replacement_tokens} tokens); use a shorter summary "
                "or reason"
            )
        return ContextReplacement(
            handle=str(entry.handle),
            mode=mode,
            kind=entry.kind,
            op_id=entry.op_id,
            step=entry.step,
            label=entry.label,
            note=note.strip(),
            original_message=copy.deepcopy(entry.message),
            replacement_message=replacement_message,
            original_approx_tokens=entry.approx_tokens,
            original_chars=entry.chars,
            replacement_approx_tokens=replacement_tokens,
            replacement_chars=replacement_chars,
        )

    def apply_replacements(self, replacements: list[ContextReplacement]) -> None:
        """Apply a validated batch atomically with respect to this in-memory transcript."""
        if not replacements:
            raise AgentError("context replacement requires at least one item")
        by_handle = {item.handle: item for item in replacements}
        if len(by_handle) != len(replacements):
            raise AgentError("context replacement handles must be unique")

        entries = {str(entry.handle): entry for entry in self._entries if entry.handle is not None}
        # Validate every plan before mutating any slot. This matters if a future caller ever holds a
        # plan across another controller mutation; one stale item must not partially apply a batch.
        for handle, replacement in by_handle.items():
            entry = entries.get(handle)
            if (
                entry is None
                or entry.state != "raw"
                or entry.message != replacement.original_message
            ):
                raise AgentError(f"context replacement plan for {handle} is stale")

        for handle, replacement in by_handle.items():
            entry = entries[handle]
            entry.message = copy.deepcopy(replacement.replacement_message)
            entry.approx_tokens = replacement.replacement_approx_tokens
            entry.chars = replacement.replacement_chars
            entry.state = replacement.mode

    def plan_tombstones(self, handles: list[str], reason: str) -> list[ContextReplacement]:
        """Compatibility helper for one shared reason across multiple tombstones."""
        return self.plan_tombstone_items([
            {"id": handle, "reason": reason} for handle in handles
        ])

    def plan_tombstone_items(self, items: list[dict[str, str]]) -> list[ContextReplacement]:
        """Validate/build per-item tombstones without mutating the transcript."""
        handles = [str(item.get("id", "")) for item in items]
        active = self._validate_handles(handles, action="drop_context")
        replacements: list[ContextReplacement] = []
        for item in items:
            handle = str(item["id"])
            reason = str(item["reason"]).strip()
            limit = self._tombstone_limit_chars(active[handle])
            if limit <= 0:
                raise AgentError(f"drop_context cannot reduce {handle}; leave the tiny raw item protected")
            if len(reason) > limit:
                raise AgentError(
                    f"drop_context reason for {handle} must be at most {limit} characters "
                    "for this raw item"
                )
            replacements.append(
                self._make_replacement(active[handle], mode="tombstone", note=reason)
            )
        return replacements

    def tombstone(self, handles: list[str], reason: str) -> list[ContextReplacement]:
        """Replace selected raw entries in their existing chronological slots with tombstones."""
        replacements = self.plan_tombstones(handles, reason)
        self.apply_replacements(replacements)
        return replacements

    def plan_compactions(self, items: list[dict[str, str]]) -> list[ContextReplacement]:
        """Validate/build concise summary replacements without mutating the transcript."""
        handles = [str(item.get("id", "")) for item in items]
        active = self._validate_handles(handles, action="compact_context")
        replacements: list[ContextReplacement] = []
        for item in items:
            handle = str(item["id"])
            summary = str(item["summary"]).strip()
            limit = self._summary_limit_chars(active[handle])
            if limit <= 0:
                raise AgentError(
                    f"compact_context cannot reduce {handle}; use drop_context if it is obsolete"
                )
            if len(summary) > limit:
                raise AgentError(
                    f"compact_context summary for {handle} must be at most {limit} characters "
                    "for this raw item"
                )
            replacements.append(
                self._make_replacement(active[handle], mode="compacted", note=summary)
            )
        return replacements

    def compact(self, items: list[dict[str, str]]) -> list[ContextReplacement]:
        """Replace raw entries in place with Worker-authored concise summaries, atomically."""
        replacements = self.plan_compactions(items)
        # Nothing has been mutated until every requested replacement validates.
        self.apply_replacements(replacements)
        return replacements

    def drop(self, handles: list[str]) -> list[ContextItem]:
        """Legacy destructive helper retained for compatibility tests/callers.

        Runtime cleanup no longer calls this method; ``drop_context`` now uses ``tombstone``.
        """
        active = self._validate_handles(handles, action="drop_context")

        wanted = set(handles)
        dropped: list[ContextItem] = []
        kept: list[_Entry] = []
        for entry in self._entries:
            if entry.handle in wanted and self._is_replaceable_entry(entry):
                dropped.append(ContextItem(
                    handle=str(entry.handle),
                    kind=entry.kind,
                    op_id=entry.op_id,
                    step=entry.step,
                    approx_tokens=entry.approx_tokens,
                    chars=entry.chars,
                    label=entry.label,
                    max_tombstone_reason_chars=self._tombstone_limit_chars(entry),
                    max_summary_chars=self._summary_limit_chars(entry),
                ))
            else:
                kept.append(entry)
        self._entries = kept
        return dropped

    def telemetry(self) -> dict[str, int]:
        """Return controller-side size accounting for the transient Worker transcript.

        ``total_*`` covers every message retained in ``WorkingContext``, including replacements.
        ``removable_*`` is the still-raw subset exposed through context-replacement actions.
        ``replacement_*`` measures tombstones/summaries that already spent their one replacement
        capability. ``protected_*`` includes those replacements plus transient material that never
        had a capability. The maintenance gate keys only off ``removable_approx_tokens`` so the
        Worker is never blocked for bytes it is structurally unable to replace again.
        """
        items = self.removable_items()
        working_tokens = sum(entry.approx_tokens for entry in self._entries)
        total_chars = sum(entry.chars for entry in self._entries)
        removable_tokens = sum(item.approx_tokens for item in items)
        removable_chars = sum(item.chars for item in items)
        replacement_entries = [entry for entry in self._entries if entry.state != "raw"]
        replacement_tokens = sum(entry.approx_tokens for entry in replacement_entries)
        replacement_chars = sum(entry.chars for entry in replacement_entries)
        return {
            "active_messages": len(self._entries),
            "total_approx_tokens": working_tokens,
            "total_chars": total_chars,
            "removable_items": len(items),
            "removable_approx_tokens": removable_tokens,
            "removable_chars": removable_chars,
            "replacement_items": len(replacement_entries),
            "replacement_approx_tokens": replacement_tokens,
            "replacement_chars": replacement_chars,
            "protected_items": len(self._entries) - len(items),
            "protected_approx_tokens": max(0, working_tokens - removable_tokens),
            "protected_chars": max(0, total_chars - removable_chars),
        }


def _inventory_lines(context: WorkingContext, *, max_items: int = 12) -> tuple[list[str], int]:
    items = context.removable_items()
    shown = items[-max_items:]
    lines: list[str] = []
    for item in shown:
        label = item.label.replace("\n", " ").strip()
        if len(label) > 120:
            label = label[:117] + "..."
        suffix = f" | {label}" if label else ""
        op = f" {item.op_id}" if item.op_id else ""
        drop = (
            f", drop-reason<={item.max_tombstone_reason_chars} chars"
            if item.max_tombstone_reason_chars > 0 else ", drop unavailable"
        )
        compact = (
            f", compact<={item.max_summary_chars} chars"
            if item.max_summary_chars > 0 else ", compact unavailable"
        )
        lines.append(
            f"{item.handle}{op}: {item.kind}, ~{item.approx_tokens} tokens, {item.chars} chars"
            f"{drop}{compact}{suffix}"
        )
    return lines, len(items) - len(shown)


def context_inventory_message(context: WorkingContext, *, max_items: int = 12) -> dict[str, str] | None:
    """Neutral, ephemeral inventory of raw material the Worker is allowed to replace.

    The caller reconstructs this message for the current provider request only. It is never appended
    to ``WorkingContext``, so yesterday's inventory cannot itself accumulate into tomorrow's context.
    """
    items = context.removable_items()
    if not items:
        return None
    lines, hidden = _inventory_lines(context, max_items=max_items)
    hidden_note = f"\nOlder removable items not listed: {hidden}." if hidden > 0 else ""
    return {
        "role": "user",
        "content": (
            "WORKING CONTEXT INVENTORY (current-turn telemetry only; not retained). These handles "
            "identify raw working messages that Core permits you to replace in place. Use drop_context "
            "when the raw material is obsolete (it becomes a short tombstone) or compact_context when "
            "a concise fact-bearing summary must remain in that chronological slot. The exact original "
            "raw message is archived outside the Worker prompt.\n"
            + "\n".join(lines)
            + hidden_note
        ),
    }


def context_pressure_level(
    *,
    managed_tokens: int,
    managed_budget: int,
    soft_ratio: float = 0.50,
    strong_ratio: float = 0.75,
    urgent_ratio: float = 0.90,
) -> str:
    """Classify pressure of cleanup-eligible raw working context.

    This deliberately does *not* use provider ``prompt_tokens``. System/task/state layers,
    current reasoning, ephemeral control messages, and protected transient messages are not
    chargeable to the Worker because no context-replacement capability can target them. Global semantic
    compaction remains independently governed by provider-reported prompt usage.
    """
    if managed_budget <= 0:
        raise ValueError("managed_budget must be positive")
    if managed_tokens < 0:
        raise ValueError("managed_tokens must be non-negative")
    if not (0.0 < soft_ratio < strong_ratio < urgent_ratio < 1.0):
        raise ValueError("pressure ratios must satisfy 0 < soft < strong < urgent < 1")
    ratio = managed_tokens / managed_budget
    if ratio >= urgent_ratio:
        return "urgent"
    if ratio >= strong_ratio:
        return "strong"
    if ratio >= soft_ratio:
        return "soft"
    return "silent"


def context_pressure_message(
    context: WorkingContext,
    *,
    managed_budget: int,
    soft_ratio: float = 0.50,
    strong_ratio: float = 0.75,
    urgent_ratio: float = 0.90,
    max_items: int = 12,
) -> tuple[dict[str, str] | None, str]:
    """Build exactly one ephemeral cleanup-pressure snapshot.

    Pressure is based only on raw observations the Worker can still replace. All immutable,
    durable, reasoning, already-replaced, and otherwise protected prompt material is excluded from
    the gate calculation.
    The snapshot itself is never retained in ``WorkingContext``.
    """
    stats = context.telemetry()
    managed_tokens = stats["removable_approx_tokens"]
    level = context_pressure_level(
        managed_tokens=managed_tokens,
        managed_budget=managed_budget,
        soft_ratio=soft_ratio,
        strong_ratio=strong_ratio,
        urgent_ratio=urgent_ratio,
    )
    if level == "silent":
        return context_inventory_message(context, max_items=max_items), level

    pct = 100.0 * managed_tokens / managed_budget
    if level == "soft":
        advice = (
            "Cleanup-eligible working context is becoming substantial. Consider drop_context for "
            "obsolete observations or compact_context when a small useful summary should remain."
        )
    elif level == "strong":
        advice = (
            "Cleanup-eligible working-context pressure is high. Review removable observations now; "
            "use drop_context for obsolete material or compact_context for facts that still matter "
            "before acquiring more large outputs."
        )
    else:
        advice = (
            "Cleanup-eligible working-context pressure is very high. Replace raw observations now: "
            "drop_context for obsolete material, compact_context for concise information that must remain."
        )

    parts = [
        "CURRENT MANAGED CONTEXT STATUS (ephemeral current-turn guidance; not retained): "
        f"~{managed_tokens}/{managed_budget} cleanup-eligible tokens ({pct:.1f}% of managed budget). "
        f"Total transient WorkingContext is ~{stats['total_approx_tokens']} tokens, of which "
        f"~{stats['protected_approx_tokens']} are protected and do not count toward this pressure. "
        "System/task/state/reasoning and ephemeral controller messages are also excluded. "
        + advice
    ]

    items = context.removable_items()
    if items:
        lines, hidden = _inventory_lines(context, max_items=max_items)
        hidden_note = f"\nOlder removable items not listed: {hidden}." if hidden > 0 else ""
        parts.append(
            "REPLACEABLE RAW CONTEXT HANDLES (current snapshot; exact originals remain archived by Core):\n"
            + "\n".join(lines)
            + hidden_note
        )
    else:
        parts.append("No removable working-context handles are currently available.")

    return {"role": "user", "content": "\n\n".join(parts)}, level


def context_maintenance_transition(
    active: bool,
    *,
    managed_tokens: int,
    managed_budget: int,
    enter_ratio: float = 0.75,
    release_ratio: float = 0.55,
) -> bool:
    """Return whether Core's deterministic cleanup gate should be active.

    The gate measures only ``drop_context``-eligible raw observations.  It enters at
    ``enter_ratio`` of that dedicated managed budget and remains closed until cleanup reaches the
    release boundary.  Provider prompt usage is intentionally absent here; it belongs to the
    independent global semantic-compaction safety net.
    """
    if managed_budget <= 0:
        raise ValueError("managed_budget must be positive")
    if managed_tokens < 0:
        raise ValueError("managed_tokens must be non-negative")
    if not (0.0 < release_ratio < enter_ratio < 1.0):
        raise ValueError("maintenance ratios must satisfy 0 < release < enter < 1")
    ratio = managed_tokens / managed_budget
    if active:
        return ratio > release_ratio
    return ratio >= enter_ratio


def context_maintenance_gate_message(
    context: WorkingContext,
    *,
    managed_budget: int,
    enter_ratio: float = 0.75,
    release_ratio: float = 0.55,
    max_items: int = 12,
) -> dict[str, str]:
    """Build one ephemeral hard-gate instruction plus current replaceable-raw inventory."""
    if managed_budget <= 0:
        raise ValueError("managed_budget must be positive")
    if not (0.0 < release_ratio < enter_ratio < 1.0):
        raise ValueError("maintenance ratios must satisfy 0 < release < enter < 1")

    stats = context.telemetry()
    managed_tokens = stats["removable_approx_tokens"]
    items = context.removable_items()
    lines, hidden = _inventory_lines(context, max_items=max_items)
    release_tokens = int(managed_budget * release_ratio)
    enter_tokens = int(managed_budget * enter_ratio)
    hidden_note = f"\nOlder removable items not listed: {hidden}." if hidden > 0 else ""
    inventory = "\n".join(lines) if lines else "(no removable handles are currently available)"
    return {
        "role": "user",
        "content": (
            "CONTEXT MAINTENANCE GATE ACTIVE (current-turn control only; not retained).\n"
            "Core has temporarily disabled normal shell and finish actions. Before continuing the task, "
            "reduce cleanup-eligible raw working context by replacing raw handles. Choose the semantics "
            "yourself: drop_context leaves a short reason tombstone; compact_context leaves a concise "
            "summary in the same chronological slot. Normal actions remain blocked until replaceable raw "
            f"working context falls to <= {release_tokens} tokens ({100.0 * release_ratio:.0f}% of its managed budget).\n"
            f"Current cleanup-eligible load: ~{managed_tokens}/{managed_budget} tokens "
            f"({100.0 * managed_tokens / managed_budget:.1f}%). Gate entry: {enter_tokens} tokens "
            f"({100.0 * enter_ratio:.0f}%). Total transient WorkingContext: ~{stats['total_approx_tokens']} tokens; "
            f"protected transient material: ~{stats['protected_approx_tokens']} tokens and is NOT charged to the gate.\n"
            "System/task/state/current reasoning/ephemeral controller messages are also NOT charged to the gate.\n"
            "The only accepted actions while this gate is active are:\n"
            '{"action":"drop_context","items":[{"id":"W0001","reason":"inspection complete"}]}\n'
            '{"action":"compact_context","items":[{"id":"W0002","summary":"key fact to retain"}]}\n'
            "Both actions preserve the chronological slot and archive the exact original outside the "
            "Worker prompt. A replaced handle cannot be replaced again.\n"
            "REPLACEABLE RAW CONTEXT HANDLES:\n" + inventory + hidden_note
        ),
    }


def context_maintenance_action_allowed(active: bool, action_kind: str) -> bool:
    """Hard-gate policy: while active, only explicit raw-context replacement may proceed."""
    return (not active) or action_kind in {"drop_context", "compact_context"}
