"""Semantic state-manager cycle, compaction triggers, and worker-state projection."""

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

from .config import (
    DEFAULT_STATE_AFTER,
    DEFAULT_STATE_REASONING_BUDGET,
    DEFAULT_WORKER_REASONING_ATTRACTOR_TOKENS,
    DEFAULT_WORKING_THOUGHTS_CHARS,
    HYPOTHESIS_STATUS_ENUM,
    MAX_OPERATIONS_IN_PROMPT,
    SOURCE_ENUM,
    STATE_MANAGER_PROMPT,
    STATE_MANAGER_TOOLS,
    _empty_state_patch,
)
from .core import AgentError, ProviderTurn, StateCycleResult, _trim
from .provider import ChatProvider
from .state_store import StateStore

def _state_for_worker(state: dict[str, Any]) -> dict[str, Any]:
    """Task is supplied separately verbatim, so omit duplicate task/counters/metadata from prompt."""
    compacted_through = int(
        state.get("metadata", {}).get("worker_ops_compacted_through", 0) or 0
    )
    fresh_operations: list[dict[str, Any]] = []
    for operation in state.get("operations", []):
        op_id = str(operation.get("id", ""))
        op_number = int(op_id[2:]) if op_id.startswith("OP") and op_id[2:].isdigit() else None
        if op_number is None or op_number > compacted_through:
            fresh_operations.append(operation)
    return {
        "version": state.get("version", 0),
        "current_focus": state.get("current_focus", ""),
        "verified": state.get("verified", []),
        "hypotheses": state.get("hypotheses", []),
        "unresolved": state.get("unresolved", []),
        "inspected": state.get("inspected", []),
        "changes": state.get("changes", []),
        "failures": state.get("failures", []),
        "loop_signals": state.get("loop_signals", []),
        # Context replacement decisions remain in controller state/logs, but are deliberately not
        # projected here: the tombstone/summary already occupies the original chronological Worker
        # slot.  Repeating the same fact in persistent state would duplicate responsibility.
        # Full factual ledger stays on disk; once a global cycle semantically consolidates an OP,
        # the Worker no longer needs that raw operation duplicated in every prompt.
        "operations": fresh_operations[-MAX_OPERATIONS_IN_PROMPT:],
    }


def _state_for_manager(state: dict[str, Any]) -> dict[str, Any]:
    """Bounded semantic view; the current iteration already carries fresh shell evidence."""
    return {
        "version": state.get("version", 0),
        "current_focus": state.get("current_focus", ""),
        "verified": state.get("verified", []),
        "hypotheses": state.get("hypotheses", []),
        "unresolved": state.get("unresolved", []),
        "inspected": state.get("inspected", []),
        "changes": state.get("changes", []),
        "failures": state.get("failures", []),
        "loop_signals": state.get("loop_signals", []),
        "run": state.get("run", {}),
    }


def _state_message(state: dict[str, Any]) -> dict[str, str]:
    return {
        "role": "user",
        "content": (
            "CONTROLLER-MANAGED PERSISTENT WORK STATE. This is working memory, not a new task and "
            "not validator truth. The original TASK remains authoritative. Re-inspect /work if state "
            "and filesystem disagree. Do not reopen rejected hypotheses without materially new evidence.\n"
            + json.dumps(_state_for_worker(state), ensure_ascii=False)
        ),
    }


def _transcript_text(messages: list[dict[str, str]]) -> str:
    return "\n\n".join(
        f"{item.get('role', 'unknown').upper()}:\n{item.get('content', '')}" for item in messages
    )


def _decode_state_tool_call(call: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    function = call.get("function")
    if not isinstance(function, dict):
        raise AgentError("state manager returned malformed tool call")
    name = function.get("name")
    if not isinstance(name, str) or not name:
        raise AgentError("state manager returned tool call without a function name")
    raw_arguments = function.get("arguments", {})
    if isinstance(raw_arguments, str):
        try:
            arguments = json.loads(raw_arguments) if raw_arguments.strip() else {}
        except json.JSONDecodeError as exc:
            raise AgentError(f"state manager returned invalid arguments for {name}: {exc}") from exc
    elif isinstance(raw_arguments, dict):
        arguments = raw_arguments
    else:
        raise AgentError(f"state manager returned invalid arguments for {name}")
    if not isinstance(arguments, dict):
        raise AgentError(f"state manager arguments for {name} are not an object")
    return name, arguments


def _state_patch_from_tool_calls(tool_calls: list[dict[str, Any]]) -> dict[str, Any]:
    """Translate semantic Manager tool calls into the controller's private patch format."""
    if not tool_calls:
        raise AgentError("state manager returned no state tool calls")

    patch = _empty_state_patch()
    saw_no_change = False
    mutation_count = 0

    def require_string(args: dict[str, Any], key: str, tool: str, *, allow_empty: bool = False) -> str:
        value = args.get(key)
        if not isinstance(value, str) or (not allow_empty and not value.strip()):
            raise AgentError(f"state manager tool {tool} requires non-empty string {key}")
        return value.strip() if not allow_empty else value

    def require_source(args: dict[str, Any], tool: str) -> str:
        source = args.get("source")
        if source not in SOURCE_ENUM:
            raise AgentError(f"state manager tool {tool} has invalid source")
        return str(source)

    for call in tool_calls:
        name, args = _decode_state_tool_call(call)
        if name == "no_state_change":
            if args:
                raise AgentError("no_state_change does not accept arguments")
            saw_no_change = True
            continue
        mutation_count += 1

        if name == "set_focus":
            if patch["focus_update"]["set"]:
                raise AgentError("state manager called set_focus more than once")
            patch["focus_update"] = {
                "set": True,
                "value": require_string(args, "value", name, allow_empty=True),
            }
        elif name == "add_verified":
            patch["verified_add"].append({
                "text": require_string(args, "text", name),
                "basis": require_string(args, "basis", name),
                "source": "transcript",
            })
        elif name == "add_hypothesis":
            status = args.get("status")
            if status not in HYPOTHESIS_STATUS_ENUM:
                raise AgentError("state manager tool add_hypothesis has invalid status")
            patch["hypotheses_add"].append({
                "text": require_string(args, "text", name),
                "status": status,
                "basis": require_string(args, "basis", name),
                "source": require_source(args, name),
            })
        elif name == "update_hypothesis":
            status = args.get("status")
            if status not in HYPOTHESIS_STATUS_ENUM:
                raise AgentError("state manager tool update_hypothesis has invalid status")
            patch["hypotheses_update"].append({
                "id": require_string(args, "id", name),
                "status": status,
                "basis": require_string(args, "basis", name),
            })
        elif name == "add_unresolved":
            patch["unresolved_add"].append({
                "text": require_string(args, "text", name),
                "basis": require_string(args, "basis", name),
                "source": require_source(args, name),
            })
        elif name == "resolve_unresolved":
            patch["unresolved_resolve"].append({
                "id": require_string(args, "id", name),
                "resolution": require_string(args, "resolution", name),
                "basis": require_string(args, "basis", name),
            })
        elif name == "add_inspected":
            patch["inspected_add"].append({
                "path": require_string(args, "path", name),
                "basis": require_string(args, "basis", name),
                "source": require_source(args, name),
            })
        elif name == "add_change":
            patch["changes_add"].append({
                "text": require_string(args, "text", name),
                "basis": require_string(args, "basis", name),
                "source": require_source(args, name),
            })
        elif name == "add_failure":
            patch["failures_add"].append({
                "text": require_string(args, "text", name),
                "basis": require_string(args, "basis", name),
                "source": require_source(args, name),
            })
        elif name == "add_loop_signal":
            patch["loop_signals_add"].append({
                "text": require_string(args, "text", name),
                "basis": require_string(args, "basis", name),
                "source": require_source(args, name),
            })
        else:
            raise AgentError(f"state manager called unknown state tool {name}")

    if saw_no_change and mutation_count:
        raise AgentError("state manager mixed no_state_change with mutation tools")
    if not saw_no_change and mutation_count == 0:
        raise AgentError("state manager returned no usable state mutations")
    return patch


def propose_state_patch(
    provider: ChatProvider,
    *,
    task: str,
    state: dict[str, Any],
    transcript: list[dict[str, str]],
    pending_reasoning: list[dict[str, Any]],
    worker_reasoning: str = "",
    reasoning_budget: int = DEFAULT_STATE_REASONING_BUDGET,
    correction_error: str = "",
) -> dict[str, Any]:
    payload_parts = [
        "ORIGINAL TASK (authoritative and immutable):\n" + task,
        "CURRENT SEMANTIC STATE VIEW (the controller keeps the complete operation ledger separately):\n"
        + json.dumps(_state_for_manager(state), ensure_ascii=False),
    ]
    if transcript:
        payload_parts.append("NEW EXECUTION TRANSCRIPT:\n" + _transcript_text(transcript))
    if worker_reasoning.strip():
        payload_parts.append(
            "WORKER REASONING FROM THIS ITERATION. It is useful search context but NOT verified evidence:\n"
            + _trim(worker_reasoning, DEFAULT_WORKING_THOUGHTS_CHARS)
        )
    if correction_error.strip():
        payload_parts.append(
            "CORE VALIDATION REJECTED YOUR PREVIOUS STATE MUTATION. Correct only the rejected "
            "semantic mutation against the SAME iteration and SAME provisional state. Do not invent "
            "new evidence. Exact Core error:\n" + correction_error.strip()
        )
    if pending_reasoning:
        compact_pending = [
            {
                "finish_reason": item.get("finish_reason"),
                "reasoning": item.get("reasoning", ""),
            }
            for item in pending_reasoning
        ]
        payload_parts.append(
            "UNASSIMILATED FAILED WORKER REASONING. This is unverified reasoning memory, not truth:\n"
            + json.dumps(compact_pending, ensure_ascii=False)
        )
    turn = provider.stateless_completion(
        [
            {"role": "system", "content": STATE_MANAGER_PROMPT},
            {"role": "user", "content": "\n\n".join(payload_parts)},
        ],
        thinking_budget_tokens=reasoning_budget,
        reasoning_effort="medium",
        tools=STATE_MANAGER_TOOLS,
        tool_choice="required",
    )
    return _state_patch_from_tool_calls(list(turn.tool_calls or []))


def assimilate_state(
    provider: ChatProvider,
    store: StateStore,
    *,
    task: str,
    transcript: list[dict[str, str]],
    verbose: bool,
    pending_reasoning: list[dict[str, Any]] | None = None,
    reasoning_budget: int = DEFAULT_STATE_REASONING_BUDGET,
) -> bool:
    """Best-effort bounded transaction for exactly one new assimilation unit."""
    pending_reasoning = list(pending_reasoning or [])
    try:
        state = store.load()
        patch = propose_state_patch(
            provider,
            task=task,
            state=state,
            transcript=transcript,
            pending_reasoning=pending_reasoning,
            reasoning_budget=reasoning_budget,
        )
        new_state, applied = store.apply_patch(patch)
        pending_ids = {
            str(item.get("id"))
            for item in pending_reasoning
            if isinstance(item.get("id"), str) and item.get("id")
        }
        store.remove_pending_reasoning(pending_ids)
        if verbose:
            print(
                f"[state] v{state.get('version', 0)} -> v{new_state.get('version', 0)}; "
                f"applied {len(applied)} change(s)",
                file=sys.stderr,
            )
            if applied:
                print(f"[state] ids: {', '.join(applied)}", file=sys.stderr)
        return True
    except AgentError as exc:
        store.record_manager_failure(str(exc))
        store.record_failed_assimilation(
            transcript=transcript,
            pending_reasoning=pending_reasoning,
            error=str(exc),
        )
        if verbose:
            print(
                f"[state] manager failed; bounded unit preserved without automatic retry: {exc}",
                file=sys.stderr,
            )
        return False



def _working_thoughts_message(text: str) -> dict[str, str]:
    return {
        "role": "user",
        "content": (
            "EPHEMERAL WORKING THOUGHTS FROM YOUR PREVIOUS ITERATION. "
            "They are not persistent truth and may be wrong. Continue from them when useful; "
            "controller-managed state and /work remain authoritative.\n"
            + _trim(text, DEFAULT_WORKING_THOUGHTS_CHARS)
        ),
    }


def run_state_cycle(
    provider: ChatProvider,
    store: StateStore,
    *,
    task: str,
    verbose: bool,
    reasoning_budget: int = DEFAULT_STATE_REASONING_BUDGET,
    pending: list[dict[str, Any]] | None = None,
    continuation_state: dict[str, Any] | None = None,
) -> StateCycleResult:
    """Process iterations with the existing tools, validation and bounded failure policy.

    Window recovery supplies eligible inputs and a transient continuation state;
    the same engine returns and audits its output without publishing semantic history.
    Default callers retain the original durable state-cycle behavior.
    """
    pending = store.load_pending_iterations() if pending is None else pending
    if not pending:
        state = store.load() if continuation_state is None else copy.deepcopy(continuation_state)
        return StateCycleResult(state, [], None, [])

    base_state = store.load() if continuation_state is None else copy.deepcopy(continuation_state)
    provisional = copy.deepcopy(base_state)
    processed: list[str] = []
    quarantined: list[str] = []
    processed_reasoning_ids: list[str] = []
    applied_ids: list[str] = []
    failed_iteration: str | None = None

    store._append_log({
        "kind": "state_cycle_started",
        "base_version": base_state.get("version", 0),
        "pending": [item.get("id") for item in pending],
    })

    for iteration in pending:
        iteration_id = str(iteration.get("id", ""))
        transcript = iteration.get("transcript", [])
        if not isinstance(transcript, list):
            transcript = []
        worker_reasoning = str(iteration.get("worker_reasoning", ""))
        reasoning_id = iteration.get("reasoning_id")
        pending_reasoning: list[dict[str, Any]] = []
        if isinstance(reasoning_id, str) and reasoning_id:
            pending_reasoning = [
                item for item in store.load_pending_reasoning()
                if item.get("id") == reasoning_id
            ]

        if verbose:
            print(f"[state-cycle] {iteration_id}: manager", file=sys.stderr)
        try:
            correction_error = ""
            applied: list[str] | None = None
            for attempt in range(1, 4):
                patch = propose_state_patch(
                    provider,
                    task=task,
                    state=provisional,
                    transcript=transcript,
                    pending_reasoning=pending_reasoning,
                    worker_reasoning="" if pending_reasoning else worker_reasoning,
                    reasoning_budget=reasoning_budget,
                    correction_error=correction_error,
                )
                try:
                    candidate, candidate_applied = store.apply_patch(
                        patch,
                        base_state=provisional,
                        persist=False,
                        bump_version=False,
                    )
                except AgentError as exc:
                    correction_error = str(exc)
                    store._append_log({
                        "kind": "state_iteration_core_rejected",
                        "iteration": iteration_id,
                        "attempt": attempt,
                        "error": correction_error,
                    })
                    if attempt < 3:
                        if verbose:
                            print(
                                f"[state-cycle] {iteration_id}: Core rejected semantic mutation; "
                                f"corrective attempt {attempt + 1}/3: {correction_error}",
                                file=sys.stderr,
                            )
                        continue
                    raise
                provisional = candidate
                applied = candidate_applied
                break

            assert applied is not None
            processed.append(iteration_id)
            applied_ids.extend(applied)
            if isinstance(reasoning_id, str) and reasoning_id:
                processed_reasoning_ids.append(reasoning_id)
            store._append_log({
                "kind": "state_iteration_assimilated",
                "iteration": iteration_id,
                "applied": applied,
            })
            if verbose:
                print(f"[state-cycle] {iteration_id}: ok; applied={len(applied)}", file=sys.stderr)
        except AgentError as exc:
            failed_iteration = iteration_id
            quarantined.append(iteration_id)
            store.record_manager_failure(f"{iteration_id}: {exc}")
            store.record_failed_assimilation(
                transcript=transcript,
                pending_reasoning=pending_reasoning,
                error=str(exc),
                iteration_id=iteration_id,
            )
            if isinstance(reasoning_id, str) and reasoning_id:
                processed_reasoning_ids.append(reasoning_id)
            store._append_log({
                "kind": "state_iteration_quarantined",
                "iteration": iteration_id,
                "error": str(exc),
            })
            if verbose:
                print(
                    f"[state-cycle] {iteration_id}: manager/Core failed; quarantined after bounded handling: {exc}",
                    file=sys.stderr,
                )
            break

    published = store.commit_state_cycle(
        base_state=base_state,
        provisional_state=provisional,
        processed_iterations=processed,
        applied_ids=applied_ids,
        quarantined_iterations=quarantined,
        processed_reasoning_ids=processed_reasoning_ids,
        persist=continuation_state is None,
    )
    if verbose:
        print(
            f"[state-cycle] v{base_state.get('version', 0)} -> v{published.get('version', 0)}; "
            f"processed={len(processed)} quarantined={len(quarantined)} remaining={len(store.load_pending_iterations())}",
            file=sys.stderr,
        )
    return StateCycleResult(published, processed, failed_iteration, quarantined)



def _worker_context_threshold(
    *, context_budget: int, context_ratio: float, token_limit: int | None = None
) -> int:
    """Return the prompt-token threshold used to trigger semantic compaction."""
    if token_limit is not None:
        return token_limit
    return max(1, int(context_budget * context_ratio))


def _worker_context_pressure_reason(
    turn: ProviderTurn,
    *,
    context_budget: int,
    context_ratio: float,
    token_limit: int | None = None,
    recent_messages: int = 0,
    fallback_message_limit: int = DEFAULT_STATE_AFTER,
) -> str | None:
    """Return a reason to consolidate when Worker input is becoming context-heavy.

    Prefer provider-reported prompt token usage. An explicit token limit overrides the ratio.
    The message-count fallback exists only for providers that do not report usage.
    """
    if turn.prompt_tokens is not None:
        threshold = _worker_context_threshold(
            context_budget=context_budget, context_ratio=context_ratio, token_limit=token_limit
        )
        if turn.prompt_tokens >= threshold:
            pct = 100.0 * turn.prompt_tokens / context_budget
            if token_limit is not None:
                return (
                    f"worker context pressure: prompt_tokens={turn.prompt_tokens} "
                    f">= token_limit={threshold} (provider_context={context_budget}, {pct:.1f}%)"
                )
            return (
                f"worker context pressure: prompt_tokens={turn.prompt_tokens}/{context_budget} "
                f"({pct:.1f}%) >= {context_ratio * 100.0:.1f}%"
            )
        return None
    if fallback_message_limit > 0 and recent_messages >= fallback_message_limit:
        return (
            "worker context pressure fallback: provider did not report prompt_tokens and "
            f"recent_messages={recent_messages} >= {fallback_message_limit}"
        )
    return None


def _worker_reasoning_attractor_reason(
    turn: ProviderTurn,
    *,
    threshold: int = DEFAULT_WORKER_REASONING_ATTRACTOR_TOKENS,
) -> str | None:
    """Recognize a reasoning-only exhaustion before retrying the Worker from the same state."""
    if not turn.reasoning_content.strip():
        return None
    if turn.reasoning_tokens is not None and turn.reasoning_tokens >= threshold:
        return (
            f"worker reasoning attractor: reasoning_tokens={turn.reasoning_tokens} "
            f">= {threshold}, finish_reason={turn.finish_reason or 'unknown'}"
        )
    if turn.finish_reason == "length":
        return "worker reasoning attractor: finish_reason=length with reasoning and no usable action"
    return None


def _maybe_run_state_cycle(
    provider: ChatProvider,
    store: StateStore,
    *,
    task: str,
    threshold: int,
    force: bool,
    verbose: bool,
    reasoning_budget: int = DEFAULT_STATE_REASONING_BUDGET,
    pending: list[dict[str, Any]] | None = None,
    continuation_state: dict[str, Any] | None = None,
) -> StateCycleResult | None:
    pending = store.load_pending_iterations() if pending is None else pending
    if not pending:
        return None
    # Fixed iteration cadence is now only an optional compatibility/safety fallback.
    # threshold=0 disables it; normal cycles are forced by Worker context pressure, reasoning
    # exhaustion, finish, or final shutdown.
    if not force and (threshold <= 0 or len(pending) < threshold):
        return None
    return run_state_cycle(
        provider,
        store,
        task=task,
        verbose=verbose,
        reasoning_budget=reasoning_budget,
        pending=pending,
        continuation_state=continuation_state,
    )
