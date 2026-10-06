"""Worker loop: Checkpoint snapshots and chronological, bounded Worker history."""
from __future__ import annotations

import argparse
import copy
import json
import itertools
import os
import shutil
import sys
from contextlib import nullcontext
from pathlib import Path

from .config import build_worker_system_prompt
from .core import AgentError, ProviderTurn, _extract_json_object
from .provider import ChatProvider, WorkerStreamInterrupted, ProviderContextOverflow
from .reasoning_loop import ReasoningLoopDetector, RECOVERY_MESSAGE
from .worker_contract import worker_response_format
from .experiment import ExperimentRecorder
from .working_context import WorkingContext
from .sandbox import admit_shell_result, run_shell, validate_action
from .project_state import (context_notice_message, project_state_message, review_due,
                            validate_project_action, handoff_message)
from .state_store import StateStore
from .checkpoint import validate_checkpoint
from .live import LiveConsoleRenderer
from .project_map import ProjectMap
from .gui import GUI_ACTIONS, GuiRuntime, validate_gui_action
from .expert import Expert, EXPERT_PROMPT
from .interactive import (InteractiveSession, SessionEnded, RestartWorkerTurn,
                          CHAT_PROMPT, validate_chat_action)


class PromptBudget:
    """Last successful provider measurement, never a prediction of the next request."""
    def __init__(self, context: int, completion: int, fraction: float):
        self.capacity = context
        self.high = int(context * fraction)
        self.measured: int | None = None

    def needs_checkpoint(self) -> bool:
        return self.measured is not None and self.measured >= self.high

    def observe(self, messages: list[dict[str, object]], tokens: int | None) -> None:
        self.measured = tokens if type(tokens) is int and tokens >= 0 else None

    def reset(self) -> None:
        self.measured = None

    def telemetry(self) -> dict[str, object]:
        return {"measurement_source": "provider_usage" if self.measured is not None else "unknown",
                "provider_prompt_tokens": self.measured, "context_capacity": self.capacity, "high": self.high}


def _worker_generation(
    provider: ChatProvider, messages: list[dict[str, object]], *, budget: PromptBudget,
    mode: str, max_recoveries: int, recoveries: int, step: int,
    experiment: ExperimentRecorder, live: LiveConsoleRenderer | None,
    response_format: dict[str, object], before_generation=None, reasoning_effort: str | None = None,
) -> tuple[ProviderTurn, int]:
    """Retry only transport/generation; never re-enter checkpoint/review preparation here."""
    retry = False
    while True:
        if before_generation is not None:
            before_generation()
        request_messages = copy.deepcopy(messages)
        if retry:
            request_messages.append(copy.deepcopy(RECOVERY_MESSAGE))
        experiment.record_context_preflight(step=step, phase="provider_retry" if retry else "provider",
                                            breakdown=budget.telemetry())
        detector = ReasoningLoopDetector() if mode != "off" else None

        def on_delta(kind: str, text: str):
            if live is not None and kind == "reasoning":
                live.delta(kind, text)
            if detector is not None and kind == "reasoning":
                for signal in detector.feed(text):
                    if live is not None:
                        live.reasoning_loop(signal, mode=mode, attempt=recoveries)
                    if signal.confirmed:
                        if mode == "recover":
                            return False
                        experiment.record_reasoning_loop(
                            step=step, mode=mode, signal=signal.as_dict(),
                            recovery_attempt=recoveries, interrupted=False)
            return None

        if live is not None:
            live.begin_worker()
        if before_generation is not None:
            before_generation()
        options = {"response_format": response_format}
        if reasoning_effort is not None:
            options["reasoning_effort"] = reasoning_effort
        try:
            # Non-live OFF retains the original non-streaming transport.
            if detector is not None or live is not None:
                turn = provider.worker_completion(request_messages, on_delta=on_delta, **options)
            else:
                turn = provider.worker_completion(request_messages, **options)
        except WorkerStreamInterrupted as exc:
            if mode != "recover" or detector is None or detector.confirmation is None:
                raise
            exhausted = recoveries >= max_recoveries
            experiment.record_reasoning_loop(
                step=step, mode=mode, signal=detector.confirmation.as_dict(),
                recovery_attempt=recoveries, interrupted=True, turn=exc.turn, exhausted=exhausted)
            if exhausted:
                raise AgentError(
                    f"Worker reasoning loop recovery exhausted after {max_recoveries} consecutive "
                    "recovery attempts. Project state and workdir were preserved; "
                    "no action from the interrupted generations was executed."
                ) from exc
            recoveries += 1
            retry = True
            continue
        budget.observe(request_messages, turn.prompt_tokens)
        return turn, recoveries


def _default_state_dir(workdir: Path) -> Path:
    return Path(str(workdir) + ".pavlusha-state")


def _archive_history(state_dir: Path, items: list[dict[str, object]]) -> None:
    with (state_dir / "history_archive.jsonl").open("a", encoding="utf-8") as output:
        for item in items:
            output.write(json.dumps(item, ensure_ascii=False) + "\n")
        output.flush()
        os.fsync(output.fileno())


def build_worker_messages(
    system_message: dict[str, object], task_message: dict[str, object],
    recent: WorkingContext,
    *, project_state_prompt: dict[str, object] | None = None,
    project_map_message: dict[str, object] | None = None,
    context_notice: dict[str, object] | None = None,
    checkpoint_handoff: dict[str, object] | None = None,
    gui_observation: dict[str, object] | None = None,
) -> list[dict[str, object]]:
    """Frozen checkpoint baseline followed by the chronological local delta."""
    messages = [system_message, task_message]
    if project_map_message is not None:
        messages.append(project_map_message)
    if project_state_prompt is not None:
        messages.append(project_state_prompt)
    if checkpoint_handoff is not None:
        messages.append(checkpoint_handoff)
    messages.extend(recent.messages())
    if gui_observation is not None:
        messages.append(gui_observation)
    if context_notice is not None:
        messages.append(context_notice)
    return copy.deepcopy(messages)


def run_agent(args: argparse.Namespace) -> int:
    if not getattr(args, "interactive", False):
        return _run_agent(args)
    live = LiveConsoleRenderer()
    with InteractiveSession(live) as chat:
        try:
            return _run_agent(args, chat=chat, interactive_live=live)
        except SessionEnded:
            live.chat_status("SESSION ENDED")
            return 0


def _run_agent(args: argparse.Namespace, *, chat=None, interactive_live=None) -> int:
    expert = Expert(args) if getattr(args, "expert", "off") == "on" else None
    if shutil.which("bwrap") is None:
        raise AgentError("bubblewrap is not installed (expected executable: bwrap)")

    workdir = Path(args.workdir).expanduser().resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    if not workdir.is_dir():
        raise AgentError(f"workdir is not a directory: {workdir}")

    if chat is not None and not args.task:
        initial = WorkingContext()
        chat.boundary(initial, force=True)
        task = initial.messages()[0]["content"].split("\n", 1)[1] if initial.messages() else ""
    else:
        task = " ".join(args.task).strip() if args.task else sys.stdin.read().strip()
    if not task:
        raise AgentError("task is empty")

    state_dir = Path(args.state_dir).expanduser().resolve() if args.state_dir else _default_state_dir(workdir)
    try:
        state_dir.relative_to(workdir)
    except ValueError:
        pass
    else:
        raise AgentError("--state-dir must be outside --workdir so the sandboxed worker cannot edit it")

    store = StateStore(state_dir, task, reset=args.reset_state, cold_restart=True)
    experiment = ExperimentRecorder(state_dir, reset=args.reset_state)
    project_map = (
        ProjectMap(workdir, state_dir / "project_map.json", reset=args.reset_state)
        if getattr(args, "project_map", "off") == "on"
        else None
    )
    if args.verbose:
        state = store.load()
        print(f"[state] file: {store.state_path}", file=sys.stderr)
        print(f"[state] log:  {store.log_path}", file=sys.stderr)
        print(f"[state] starting at v{state.get('version', 0)}", file=sys.stderr)

    provider = ChatProvider(
        base_url=args.base_url,
        model=args.model,
        api_key=args.api_key,
        timeout=args.api_timeout,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
    )
    if args.worker_context_budget is None:
        args.worker_context_budget = provider.discover_context_length()
        if args.verbose:
            print(
                f"[worker-context] discovered active LM Studio context_length={args.worker_context_budget}",
                file=sys.stderr,
            )

    budget = PromptBudget(args.worker_context_budget, args.max_tokens, args.history_context_high)

    network_note = (
        "Network permission is granted for this run. Each shell or gui_start action must "
        "still opt in with network=true to enable network access."
        if args.network
        else "Network access has NOT been granted. Always use network=false, including gui_start."
    )
    project_map_note = (
        "\n\nA controller-generated PROJECT MAP is supplied as a checkpoint navigation snapshot. "
        "It is refreshed at checkpoints and may lag subsequent edits. It never overrides the filesystem."
        if project_map is not None else ""
    )
    system_message = {
        "role": "system",
        "content": build_worker_system_prompt(gui_enabled=bool(getattr(args, "gui", False))) + "\n\n" + network_note + project_map_note + ("\n\n" + EXPERT_PROMPT if expert else "") + (CHAT_PROMPT if chat else ""),
    }
    task_message = {"role": "user", "content": "TASK:\n" + task}

    recent = WorkingContext()
    project_state = store.get_project_state()
    state_prompt = project_state_message(project_state)
    map_prompt = None
    checkpoint_active = False
    handoff_prompt = handoff_message(store.load().get("checkpoint_handoff", ""))
    pending_context_notice: dict[str, str] | None = None
    invalid_replies = 0
    consecutive_reasoning_recoveries = 0
    consecutive_loop_recoveries = 0
    live = interactive_live or (LiveConsoleRenderer() if getattr(args, "live", False) else None)
    if live is not None:
        expert_status = ("off" if expert is None else
                         "enabled · network not granted" if not expert.network else
                         "enabled · call limit 0" if expert.max_calls == 0 else "available")
        live.start(model=provider.resolve_model(), task=task, max_steps=args.max_steps,
                   context=args.worker_context_budget, expert_status=expert_status)

    gui_runtime = (
        GuiRuntime(
            workdir, state_dir, max_command_timeout=args.command_timeout,
            network_allowed=bool(args.network),
        )
        if bool(getattr(args, "gui", False)) else None
    )
    gui_observation: dict[str, object] | None = None
    overflow_recovered_generations: set[str] = set()

    if chat is not None and args.task:
        live.chat_message("USER", task)

    with (gui_runtime if gui_runtime is not None else nullcontext()):
        for step in itertools.count(1):
            if args.max_steps != -1 and step > args.max_steps:
                break
            if args.verbose:
                print(f"[agent] step {step}/{args.max_steps}", file=sys.stderr)
            recent.current_step = step
            if chat is not None:
                chat.boundary(recent)
            # HIGH now requests a fresh checkpoint, never a destructive partial cut.
            # Keep all steps (including review attempts) until that checkpoint succeeds.
            history_overflow = recent.step_count() >= args.history_high

            state_snapshot = store.load()
            operation_count = int(state_snapshot.get("counters", {}).get("operation", 0) or 0)
            project_state = copy.deepcopy(state_snapshot.get("project_state", project_state))
            # Refresh startup Map before measuring the actual next request.
            if step == 1 and project_map is not None:
                project_map.refresh()
                map_prompt = project_map.message()
            periodic_prompt = None
            if review_due(project_state, operation_count=operation_count, every=args.project_review_every):
                periodic_prompt = {"role": "user", "content": (
                    f"PERIODIC PROJECT STATE REVIEW\n{args.project_review_every} shell operations completed since the last review.\n"
                    "This is an attention questionnaire, NOT a history checkpoint. Normal shell/finish actions are temporarily unavailable for this one review turn. "
                    "If durable DESIGN/WORK/DEVIATION information changed, record it with project_update. "
                    "If nothing durable needs recording, call project_review_skip. Do not call project_review_complete."
                )}
            history_reason = (
                f"recent history reached {args.history_high} complete Worker steps; "
                "review durable recovery information before the full history reset"
            )
            context_reason = (
                "provider-reported prompt usage reached the configured HIGH fraction of model context; "
                "review durable recovery information before the full history reset"
            )
            context_high = budget.needs_checkpoint()
            checkpoint_reason = (
                history_reason
                if history_overflow else (
                    context_reason
                    if context_high or checkpoint_active else None
                )
            )
            periodic_review_due = (
                not checkpoint_reason and periodic_prompt is not None
            )
            # Keep the early snapshots frozen throughout HIGH. Canonical updates
            # remain visible in History; refresh snapshots only after completion.
            checkpoint_active = bool(checkpoint_reason)

            messages = build_worker_messages(
                system_message, task_message, recent,
                project_state_prompt=state_prompt,
                project_map_message=map_prompt,
                context_notice=pending_context_notice,
                checkpoint_handoff=handoff_prompt,
                gui_observation=gui_observation,
            )
            if checkpoint_reason and project_state.get("initialized"):
                # The schema blocks shell during HIGH. Keep its exit protocol
                # after History too, especially after accepted project_update
                # results that otherwise look like permission to resume work.
                messages.append({"role": "user", "content": (
                    "CURRENT RUNTIME PHASE: HIGH CHECKPOINT\nPROJECT CHECKPOINT REQUIRED\n"
                    f"reason: {checkpoint_reason}\n"
                    "Only project_update and project_review_complete are available; shell/finish are unavailable. "
                    "A successful project_update does not complete this checkpoint or unlock normal work. "
                    "Record only remaining durable changes; do not repeat already accepted updates. "
                    "If there are no remaining durable changes, call project_review_complete now, "
                    "with a short handoff for the next action. This saves recovery state; "
                    "it does not finish the task or require all WORK items to be DONE. "
                    "After Core accepts it, normal actions become available on the next turn."
                )})
            elif periodic_review_due:
                messages.append(periodic_prompt)
            experiment.record_context_preflight(step=step, phase="assembled", breakdown=budget.telemetry())
            pending_context_notice = None
            context_stats = recent.telemetry()
            if live is not None:
                live.step(step, args.max_steps)

            # Cold overflow recovery clears History. Fail explicitly rather than lose/replay chat.
            interactive_evidence = chat is not None and any(m.get("content", "").startswith(
                "USER MESSAGE AT SAFE BOUNDARY:\n") for m in recent.messages())

            def before_generation():
                if chat is not None and chat.boundary(recent):
                    raise RestartWorkerTurn()

            try:
                turn, consecutive_loop_recoveries = _worker_generation(
                    provider, messages, budget=budget, mode=args.reasoning_loop_recovery,
                    max_recoveries=args.max_reasoning_loop_recoveries,
                    recoveries=consecutive_loop_recoveries, step=step, experiment=experiment, live=live,
                    response_format=worker_response_format(
                        initialized=bool(project_state.get("initialized")),
                        checkpoint_required=bool(checkpoint_reason), periodic_review=periodic_review_due,
                        gui_enabled=bool(getattr(args, "gui", False)), expert_enabled=expert is not None, interactive=chat is not None,
                    ), before_generation=before_generation if chat is not None else None,
                    reasoning_effort=getattr(args, "reasoning_effort", None),
                )
            except RestartWorkerTurn:
                continue
            except ProviderContextOverflow as exc:
                if interactive_evidence:
                    raise AgentError("Context overflow with interactive evidence in Recent History; "
                                     "stopped without discarding or replaying user messages. "
                                     "/work and committed checkpoint preserved.") from exc
                # Exactly the strict startup recovery source, never the failed history.
                checkpoint = validate_checkpoint(store.load(), task)
                identity = checkpoint["sha256"]
                if identity in overflow_recovered_generations:
                    raise AgentError("context overflow recovery exhausted: this committed checkpoint "
                                     "already recovered once; /work and factual ledger preserved") from exc
                overflow_recovered_generations.add(identity)
                store = StateStore(state_dir, task, cold_restart=True)
                project_state = store.get_project_state()
                state_prompt = project_state_message(project_state)
                handoff_prompt = handoff_message(store.load().get("checkpoint_handoff", ""))
                if project_map is not None:
                    project_map.refresh()
                    map_prompt = project_map.message()
                recent = WorkingContext()
                budget.reset()
                checkpoint_active = False
                invalid_replies = consecutive_reasoning_recoveries = consecutive_loop_recoveries = 0
                gui_observation = None
                if gui_runtime is not None:
                    gui_runtime.close()
                pending_context_notice = context_notice_message(
                    "Provider rejected a request for real context overflow. Restarted from the last committed "
                    "checkpoint with empty Recent History. /work remains current; observe it before continuing.")
                experiment.record_context_recovery(step=step, checkpoint=identity, error=str(exc))
                if live is not None:
                    live.prefix_changed("provider context overflow; cold checkpoint recovery")
                continue
            experiment.record_worker_turn(
                step=step,
                turn=turn,
                meter_mode="hidden",
                context_budget=args.worker_context_budget,
                compaction_threshold=None,
                context_control="off",
                context_stats=context_stats,
            )
            raw = turn.content
            if args.verbose and (
                turn.prompt_tokens is not None
                or turn.completion_tokens is not None
                or turn.reasoning_tokens is not None
            ):
                prompt_usage = str(turn.prompt_tokens) if turn.prompt_tokens is not None else "?"
                completion_usage = str(turn.completion_tokens) if turn.completion_tokens is not None else "?"
                reasoning_usage = str(turn.reasoning_tokens) if turn.reasoning_tokens is not None else "?"
                pct = (
                    f" ({100.0 * turn.prompt_tokens / args.worker_context_budget:.1f}% context)"
                    if turn.prompt_tokens is not None else ""
                )
                print(
                    f"[worker-usage] prompt={prompt_usage}/{args.worker_context_budget}{pct} "
                    f"completion={completion_usage} reasoning={reasoning_usage}",
                    file=sys.stderr,
                )

            if live is not None:
                live.usage(prompt=turn.prompt_tokens, context_budget=args.worker_context_budget, completion=turn.completion_tokens, reasoning=turn.reasoning_tokens)

            if chat is not None and chat.boundary(recent):
                # Proposed action has not entered dispatch; regenerate with the intervention.
                continue

            if turn.reasoning_content.strip():
                recent.append({"role": "user", "content": (
                    "WORKER REASONING (working hypotheses, not project truth):\n" + turn.reasoning_content
                )}, kind="reasoning")
            if not raw.strip():
                if (not args.no_reasoning_recovery and turn.reasoning_content.strip()
                        and consecutive_reasoning_recoveries < args.max_reasoning_recoveries):
                    consecutive_reasoning_recoveries += 1
                    recent.append({"role": "user", "content": (
                        "The previous worker attempt produced no final action"
                        + (f" (finish_reason={turn.finish_reason})" if turn.finish_reason else "")
                        + ". Its reasoning was retained for continuation. Continue and produce one valid JSON action."
                    )})
                    continue
                detail = f"finish_reason={turn.finish_reason}" if turn.finish_reason else "no finish_reason"
                if turn.reasoning_content.strip():
                    detail += f", reasoning_chars={len(turn.reasoning_content)}"
                raise AgentError(f"provider returned empty assistant content ({detail})")

            try:
                if turn.finish_reason == "length":
                    raise AgentError("Worker completion was truncated at its token ceiling; no action was executed")
                action = _extract_json_object(raw)
                requested_kind = action.get("action")
                if requested_kind in {"message", "wait_for_user"}:
                    if chat is None:
                        raise AgentError("chat actions require --interactive")
                    kind, data = validate_chat_action(action)
                elif requested_kind == "ask_expert" and expert is not None:
                    kind, data = "ask_expert", {"question": action.get("question"), "context": action.get("context")}
                elif requested_kind in {"project_init", "project_update", "project_review_skip", "project_review_complete"}:
                    kind, data = validate_project_action(action)
                elif requested_kind in GUI_ACTIONS:
                    if not bool(getattr(args, "gui", False)):
                        raise AgentError(
                            f"GUI action {requested_kind!r} requires the controller to be started with --gui"
                        )
                    kind, data = validate_gui_action(action, args.command_timeout)
                else:
                    if bool(getattr(args, "gui", False)) and requested_kind not in {
                        "shell", "finish", "drop_context", "compact_context"
                    }:
                        allowed_gui = ", ".join(sorted(GUI_ACTIONS))
                        raise AgentError(
                            f"unknown action {requested_kind!r}; GUI actions ARE enabled in this run. "
                            f"Use the JSON field 'action'. GUI actions: {allowed_gui}. "
                            "For example: {\"action\":\"gui_start\",\"command\":\"python app.py\",\"network\":false,\"timeout\":300,\"delay\":0.8}"
                        )
                    kind, data = validate_action(action, args.command_timeout)
                invalid_replies = 0
                consecutive_reasoning_recoveries = 0
            except AgentError as exc:
                if live is not None:
                    live.invalid(str(exc))
                invalid_replies += 1
                recent.append({"role": "assistant", "content": raw})
                recent.append({"role": "user", "content": (
                    "INVALID ACTION: " + str(exc) + ". Reply again with exactly one valid JSON action and no prose."
                )})
                if invalid_replies >= 3:
                    raise AgentError("model returned invalid actions three times in a row") from exc
                continue

            # Core-owned Project State gates. An uninitialized project must be materialized before
            # normal work. A required checkpoint may be updated in multiple atomic batches, but shell
            # and finish remain blocked until project_review_complete.
            if not project_state.get("initialized") and kind not in {"project_init", "message", "wait_for_user"}:
                recent.append({"role": "assistant", "content": json.dumps(action, ensure_ascii=False)})
                recent.append({"role": "user", "content": (
                    "PROJECT CHECKPOINT REQUIRED: Project State is uninitialized. "
                    "Call project_init before shell or finish."
                )})
                continue
            if project_state.get("initialized") and checkpoint_reason and kind not in {"project_update", "project_review_complete", "message", "wait_for_user"}:
                recent.append({"role": "assistant", "content": json.dumps(action, ensure_ascii=False)})
                recent.append({"role": "user", "content": (
                    "PROJECT CHECKPOINT REQUIRED: " + checkpoint_reason + ". "
                    "Materialize durable changes with project_update if needed, then call project_review_complete. "
                    "Normal shell/finish actions are blocked until the checkpoint is complete."
                )})
                continue
            if project_state.get("initialized") and periodic_review_due and kind not in {"project_update", "project_review_skip", "message", "wait_for_user"}:
                recent.append({"role": "assistant", "content": json.dumps(action, ensure_ascii=False)})
                recent.append({"role": "user", "content": (
                    f"PERIODIC PROJECT STATE REVIEW: {args.project_review_every} shell operations completed. "
                    "If durable DESIGN/WORK/DEVIATION information changed, record it now with project_update. "
                    "Otherwise call project_review_skip. This is not a history checkpoint; project_review_complete is unavailable here."
                )})
                continue
            if kind == "project_review_complete" and not checkpoint_reason:
                recent.append({"role": "assistant", "content": json.dumps(action, ensure_ascii=False)})
                recent.append({"role": "user", "content": (
                    "PROJECT REVIEW REJECTED: no history checkpoint is required. "
                    "project_review_complete is reserved for a Core-required HIGH history checkpoint."
                )})
                continue

            # Dispatch admission is the last safe boundary. Everything below through result
            # recording/restoration is one indivisible operation; a later signal reserves the next boundary.
            if chat is not None and chat.boundary(recent):
                continue
            assistant_message = {"role": "assistant", "content": json.dumps(action, ensure_ascii=False)}
            recent.append(assistant_message)
            experiment.record_worker_action(step=step, kind=kind, data=data)
            if live is not None:
                live.action(kind, data)
            if kind in {"message", "wait_for_user"}:
                live.chat_message("P.A.V.L.U.S.H.A.", data["text"])
                # Like every other action, close the assistant action with factual tool evidence.
                # Ending the prompt on assistant can be interpreted as assistant-prefill/EOS.
                recent.append({"role": "user", "content": "MESSAGE RESULT: user-facing text displayed."},
                              kind="message_result")
                if kind == "wait_for_user":
                    chat.boundary(recent, force=True)
                continue
            if kind == "project_init":
                try:
                    project_state = store.initialize_project(data, step=step)
                except AgentError as exc:
                    if live is not None:
                        live.invalid("PROJECT INIT REJECTED: " + str(exc))
                    invalid_replies += 1
                    recent.append({"role": "user", "content": (
                        "PROJECT INIT REJECTED: " + str(exc) + ". Revise the initial plan and call project_init again."
                    )})
                    if invalid_replies >= 3:
                        raise AgentError("Project State initialization was rejected three times in a row") from exc
                    continue
                invalid_replies = 0
                consecutive_loop_recoveries = 0
                recent.append({"role": "user", "content": "PROJECT STATE INITIALIZED:\n" + json.dumps(project_state, ensure_ascii=False)})
                state_prompt = project_state_message(project_state)
                if checkpoint_reason:
                    _archive_history(state_dir, recent.step_records())
                    recent.retain_tail_messages(0)
                    budget.reset()
                    checkpoint_active = False
                    experiment.record_prefix_changed(step=step, reason="initial checkpoint established; full history reset")
                    pending_context_notice = context_notice_message(
                        "Recent History was fully archived after a successful fresh checkpoint."
                    )
                continue

            if kind == "project_update":
                try:
                    project_state = store.update_project(data["changes"], step=step)
                except AgentError as exc:
                    if live is not None:
                        live.invalid("PROJECT UPDATE REJECTED: " + str(exc))
                    invalid_replies += 1
                    recent.append({"role": "user", "content": (
                        "PROJECT UPDATE REJECTED: " + str(exc) + ". Correct the Project State mutation; normal work has not been changed by this rejected update."
                    )})
                    if invalid_replies >= 3:
                        raise AgentError("Project State update was rejected three times in a row") from exc
                    continue
                invalid_replies = 0
                if periodic_review_due:
                    project_state = store.acknowledge_periodic_review(step=step)
                consecutive_loop_recoveries = 0
                recent.append({"role": "user", "content": "PROJECT STATE UPDATED:\n" + json.dumps(project_state, ensure_ascii=False)})
                continue

            if kind == "project_review_skip":
                if not periodic_review_due:
                    recent.append({"role": "user", "content": "PERIODIC REVIEW SKIP REJECTED: no periodic Project State review is due."})
                    continue
                project_state = store.acknowledge_periodic_review(step=step)
                consecutive_loop_recoveries = 0
                recent.append({"role": "user", "content": "PERIODIC PROJECT STATE REVIEW COMPLETE. No durable update recorded."})
                continue

            if kind == "project_review_complete":
                try:
                    project_state = store.complete_project_review(
                        step=step, note=data.get("note", ""), handoff=data["handoff"])
                except AgentError as exc:
                    if live is not None:
                        live.invalid("PROJECT REVIEW REJECTED: " + str(exc))
                    invalid_replies += 1
                    recent.append({"role": "user", "content": (
                        "PROJECT REVIEW REJECTED: " + str(exc) + ". Correct the Project State and complete the checkpoint again."
                    )})
                    if invalid_replies >= 3:
                        raise AgentError("Project State review was rejected three times in a row") from exc
                    continue
                invalid_replies = 0
                consecutive_loop_recoveries = 0
                handoff_prompt = handoff_message(data["handoff"])
                recent.append({"role": "user", "content": "PROJECT REVIEW COMPLETE. Durable recovery checkpoint accepted."})
                # Only a Core-required HIGH checkpoint may refresh the frozen authoritative prefix.
                if project_map is not None:
                    project_map.refresh()
                    map_prompt = project_map.message()
                state_prompt = project_state_message(project_state)
                checkpoint_active = False
                if live is not None:
                    live.prefix_changed("checkpoint snapshot refresh")
                if checkpoint_reason:
                    _archive_history(state_dir, recent.step_records())
                    recent.retain_tail_messages(0)
                    budget.reset()
                    checkpoint_active = False
                    experiment.record_prefix_changed(step=step, reason="checkpoint completed; full history reset")
                    pending_context_notice = context_notice_message(
                        "Recent History was fully archived after a successful fresh checkpoint."
                    )
                continue

            if kind == "ask_expert":
                def expert_telemetry(event):
                    experiment.record_expert_call(step=step, event=event)
                    if live is not None:
                        live.expert_event(event)
                result_payload = expert.ask(
                    data["question"], data["context"],
                    telemetry=expert_telemetry,
                )
                if live is not None:
                    live.expert_result(result_payload)
                recent.append({"role": "user", "content": "EXPERT RESULT:\n" +
                               json.dumps(result_payload, ensure_ascii=False)}, kind="expert_result")
                continue

            if kind in GUI_ACTIONS:
                if gui_runtime is None:
                    raise AgentError("internal GUI action accepted while --gui is disabled")
                result_payload, observation = gui_runtime.execute(kind, data)
                gui_observation = observation.message() if observation is not None else None
                if not result_payload.get("error") and result_payload.get("state") == "alive":
                    consecutive_loop_recoveries = 0
                if live is not None:
                    live.gui_result(result_payload)
                recent.append({
                    "role": "user",
                    "content": "GUI RESULT:\n" + json.dumps(result_payload, ensure_ascii=False),
                }, kind="gui_result")
                continue

            if kind == "finish":
                unfinished = [item for item in project_state.get("work", [])
                              if item.get("status") not in {"DONE", "SUPERSEDED"}]
                if unfinished:
                    recent.append({"role": "user", "content": (
                        "FINISH REJECTED: Project State still contains unfinished work: " +
                        ", ".join(f"{item.get('id')}={item.get('status')}" for item in unfinished) +
                        ". Update work status before finishing."
                    )})
                    continue
                store.mark_finished(data["summary"])
                if chat is not None:
                    live.chat_message("P.A.V.L.U.S.H.A.", data["summary"])
                elif live is not None:
                    live.complete(data["summary"])
                else:
                    print(data["summary"])
                return 0

            requested_network = data["network"]
            release_worker = bool(data.get("release_worker")) and (not requested_network or args.network)
            if release_worker:
                # A fresh generation uses the existing atomic review transaction. The selected
                # action requests cold continuity; materialized State is its recovery source.
                store.complete_project_review(step=step, note="Worker release for shell")
                validate_checkpoint(store.load(), task)
            with provider.released_worker() if release_worker else nullcontext():
                if requested_network and not args.network:
                    result_payload = {
                        "error": "network_not_granted",
                        "hint": "Run the controller with --network if the task needs dependency downloads.",
                        "command": data["command"],
                    }
                else:
                    if args.verbose:
                        net = "net" if requested_network else "offline"
                        print(f"[shell:{net}] {data['command']}", file=sys.stderr)
                    try:
                        result = run_shell(
                            workdir,
                            data["command"],
                            network=requested_network,
                            timeout=data["timeout"],
                            output_limit=args.output_limit,
                            **({"gpu": True} if data.get("gpu") else {}),
                        )
                        result_payload = admit_shell_result(result, args.output_limit)
                    except Exception as exc:
                        if not release_worker and not isinstance(exc, OSError):
                            raise
                        result_payload = {
                            "command": data["command"],
                            "network": requested_network,
                            ("launch_error" if isinstance(exc, OSError) else "execution_error"): f"{type(exc).__name__}: {exc}",
                        }

                if not requested_network or args.network:
                    consecutive_loop_recoveries = 0
                # Preserve the factual result before restore: a failed reload must not lose it
                # or cause automatic re-execution on restart.
                op_record = store.record_operation(result_payload)

            if release_worker:
                store = StateStore(state_dir, task, cold_restart=True)
                project_state = store.get_project_state()
                state_prompt = project_state_message(project_state)
                handoff_prompt = handoff_message(store.load().get("checkpoint_handoff", ""))
                if project_map is not None:
                    project_map.refresh()
                    map_prompt = project_map.message()
                _archive_history(state_dir, recent.step_records())
                recent = WorkingContext()
                recent.current_step = step
                recent.append(assistant_message)
                budget.reset()
                checkpoint_active = False
                invalid_replies = consecutive_reasoning_recoveries = consecutive_loop_recoveries = 0
                gui_observation = None
                if gui_runtime is not None:
                    gui_runtime.close()
                pending_context_notice = context_notice_message(
                    "Worker restored after intentional release for bounded shell execution. "
                    "Resumed from the fresh committed checkpoint with empty previous Recent History. "
                    "/work remains current; the factual shell result follows.")
                experiment.record_prefix_changed(step=step, reason="Worker release; cold checkpoint recovery")
                if live is not None:
                    live.prefix_changed("Worker restored; cold checkpoint recovery")
            experiment.record_operation_telemetry(step=step, op_id=op_record["id"], result=result_payload)
            if live is not None:
                live.operation(op_record["id"], result_payload)
            if args.verbose:
                print(
                    f"[state] recorded {op_record['id']} exit={op_record.get('exit_code')} "
                    f"timeout={str(op_record.get('timed_out', False)).lower()}",
                    file=sys.stderr,
                )

            result_message = {
                "role": "user",
                "content": f"SHELL RESULT ({op_record['id']}):\n" + json.dumps(result_payload, ensure_ascii=False),
            }
            recent.append(result_message, kind="shell_result", op_id=op_record["id"])

        raise AgentError(f"agent reached the maximum of {args.max_steps} steps without finishing")
