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
from .operation_loop import OperationLoopDetector
from .worker_contract import worker_response_format
from .functions import FunctionRegistry
from .experiment import ExperimentRecorder
from .working_context import WorkingContext
from .sandbox import admit_shell_result, run_shell, validate_action
from .project_state import (context_notice_message, project_state_message, review_due,
                            validate_project_action, handoff_message, empty_project_state,
                            validate_persisted_project_state)
from .state_store import StateStore
from .checkpoint import validate_checkpoint
from .live import LiveConsoleRenderer
from .project_map import ProjectMap, PythonTreeSitterIndexer
from .gui import GUI_ACTIONS, GuiError, GuiRuntime, validate_gui_action
from .expert import Expert, EXPERT_PROMPT
from .interactive import (InteractiveSession, SessionEnded, RestartWorkerTurn,
                          CHAT_PROMPT, validate_chat_action)


class PromptBudget:
    """Last successful provider measurement, never a prediction of the next request."""
    def __init__(self, context: int, fraction: float):
        self.capacity = context
        self.high = int(context * fraction)
        self.measured: int | None = None

    def needs_checkpoint(self) -> bool:
        return self.measured is not None and self.measured >= self.high

    def observe(self, tokens: int | None) -> None:
        self.measured = tokens if type(tokens) is int and tokens >= 0 else None

    def reset(self) -> None:
        self.measured = None

    def telemetry(self) -> dict[str, object]:
        return {"measurement_source": "provider_usage" if self.measured is not None else "unknown",
                "provider_prompt_tokens": self.measured, "context_capacity": self.capacity, "high": self.high}


class ReasoningLoopRecoveryExhausted(AgentError):
    """Only a confirmed lexical loop after the configured automatic retry ceiling."""
    def __init__(self, attempts: int):
        self.attempts = attempts
        super().__init__(
            f"Worker reasoning loop recovery exhausted after {attempts} consecutive "
            "recovery attempts. Project state and workdir were preserved; "
            "no action from the interrupted generations was executed.")


def _worker_generation(
    provider: ChatProvider, messages: list[dict[str, object]], *, budget: PromptBudget,
    mode: str, max_recoveries: int, recoveries: int, step: int,
    experiment: ExperimentRecorder, live: LiveConsoleRenderer | None,
    response_format: dict[str, object], before_generation=None, reasoning_effort: str | None = None,
    should_cancel=None,
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
            if should_cancel is not None and should_cancel():
                return False
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
        if should_cancel is not None:
            options["should_cancel"] = should_cancel
        try:
            # Non-live OFF retains the original non-streaming transport.
            if detector is not None or live is not None or should_cancel is not None:
                turn = provider.worker_completion(request_messages, on_delta=on_delta, **options)
            else:
                turn = provider.worker_completion(request_messages, **options)
        except WorkerStreamInterrupted as exc:
            if should_cancel is not None and should_cancel():
                experiment.record_generation_interrupted(step=step, turn=exc.turn)
                raise RestartWorkerTurn() from exc
            if mode != "recover" or detector is None or detector.confirmation is None:
                raise
            exhausted = recoveries >= max_recoveries
            experiment.record_reasoning_loop(
                step=step, mode=mode, signal=detector.confirmation.as_dict(),
                recovery_attempt=recoveries, interrupted=True, turn=exc.turn, exhausted=exhausted)
            if exhausted:
                raise ReasoningLoopRecoveryExhausted(recoveries) from exc
            recoveries += 1
            retry = True
            continue
        if should_cancel is not None and should_cancel():
            experiment.record_generation_interrupted(step=step, turn=turn)
            raise RestartWorkerTurn()
        budget.observe(turn.prompt_tokens)
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


def _validate_worker_action(turn, args, *, chat, functions, expert, gui_enabled):
    """Decode a complete reply and validate only actions enabled for this run."""
    raw = turn.content
    if turn.finish_reason == "length":
        raise AgentError("Worker completion was truncated at its token ceiling; no action was executed")
    action = _extract_json_object(raw)
    requested_kind = action.get("action")
    if requested_kind in {"message", "wait_for_user"}:
        if chat is None:
            raise AgentError("chat actions require --interactive")
        kind, data = validate_chat_action(action)
    elif requested_kind == "call_function" and functions is not None:
        kind, data = "call_function", action
    elif requested_kind == "ask_expert" and expert is not None:
        kind, data = "ask_expert", {"question": action.get("question"), "context": action.get("context")}
    elif requested_kind in {"project_init", "project_update", "project_review_skip", "project_review_complete"}:
        kind, data = validate_project_action(action)
    elif requested_kind in GUI_ACTIONS:
        if not gui_enabled:
            raise AgentError(
                f"GUI action {requested_kind!r} is unavailable: --no-gui or missing GUI dependencies"
            )
        kind, data = validate_gui_action(action)
    else:
        if gui_enabled and requested_kind not in {"shell", "finish"}:
            allowed_gui = ", ".join(sorted(GUI_ACTIONS))
            raise AgentError(
                f"unknown action {requested_kind!r}; GUI actions ARE enabled in this run. "
                f"Use the JSON field 'action'. GUI actions: {allowed_gui}. "
                "For example: {\"action\":\"gui_start\",\"command\":\"python app.py\",\"network\":false,\"delay\":0.8}"
            )
        kind, data = validate_action(action, args.command_timeout)
    return action, kind, data


def _project_action_rejection(
    kind, project_state, *, checkpoint_reason, periodic_review_due, review_every, operation_label,
):
    """Return the phase gate explanation, or admit the action for dispatch."""
    if not project_state.get("initialized") and kind not in {"project_init", "message", "wait_for_user"}:
        return (
            "PROJECT CHECKPOINT REQUIRED: Project State is uninitialized. "
            "Call project_init before shell or finish."
        )
    if project_state.get("initialized") and checkpoint_reason and kind not in {"project_update", "project_review_complete", "message", "wait_for_user"}:
        return (
            "PROJECT CHECKPOINT REQUIRED: " + checkpoint_reason + ". "
            "Materialize durable changes with project_update if needed, then call project_review_complete. "
            "Normal shell/finish actions are blocked until the checkpoint is complete."
        )
    if project_state.get("initialized") and periodic_review_due and kind not in {"project_update", "project_review_skip", "message", "wait_for_user"}:
        return (
            f"PERIODIC PROJECT STATE REVIEW: {review_every} {operation_label} operations completed. "
            "If durable DESIGN/WORK/DEVIATION information changed, record it now with project_update. "
            "Otherwise call project_review_skip. This is not a history checkpoint; project_review_complete is unavailable here."
        )
    if kind == "project_review_complete" and not checkpoint_reason:
        return (
            "PROJECT REVIEW REJECTED: no history checkpoint is required. "
            "project_review_complete is reserved for a Core-required HIGH history checkpoint."
        )

    return None


def _execute_shell_action(data, *, args, provider, store, workdir, task, step):
    """Execute and durably record before restoring a temporarily released Worker."""
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

        # Preserve the factual result before restore: a failed reload must not lose it
        # or cause automatic re-execution on restart.
        op_record = store.record_operation(result_payload)

    return result_payload, op_record, release_worker


def _recover_checkpoint_baseline(state_dir, task, project_map):
    """Reload the committed generation and rebuild its frozen prompt layers."""
    store = StateStore(state_dir, task, cold_restart=True)
    state = store.load()
    project_state = state["project_state"]
    map_prompt = None
    if project_map is not None:
        project_map.refresh()
        map_prompt = project_map.message()
    return (store, project_state, project_state_message(project_state),
            handoff_message(state["recovery_checkpoint"]["handoff"]), map_prompt)


def _reset_checkpoint_history(state_dir, recent, budget, experiment, *, step, reason):
    """Archive before clearing; a failed archive must preserve in-memory history."""
    _archive_history(state_dir, recent.step_records())
    recent.clear()
    budget.reset()
    experiment.record_prefix_changed(step=step, reason=reason)
    return context_notice_message(
        "Recent History was fully archived after a successful fresh checkpoint.")


def run_agent(args: argparse.Namespace) -> int:
    if not getattr(args, "interactive", False):
        return _run_agent(args)
    live = LiveConsoleRenderer(reasoning_loop_diagnostics=getattr(args, "reasoning_loop_diagnostics", False))
    with InteractiveSession(live) as chat:
        try:
            return _run_agent(args, chat=chat, interactive_live=live)
        except SessionEnded:
            live.chat_status("SESSION ENDED")
            return 0


def _run_agent(args: argparse.Namespace, *, chat=None, interactive_live=None) -> int:
    return _AgentRuntime(args, chat=chat, interactive_live=interactive_live).run()


class _AgentRuntime:
    """One task's resources, frozen prompt baseline, history, and action transactions."""

    def __init__(self, args: argparse.Namespace, *, chat=None, interactive_live=None) -> None:
        self.args = args
        self.chat = chat
        self.expert = Expert(self.args) if getattr(self.args, "expert", "off") == "on" else None
        self.functions = FunctionRegistry(self.args.functions) if getattr(self.args, "functions", None) else None
        if shutil.which("bwrap") is None:
            raise AgentError("bubblewrap is not installed (expected executable: bwrap)")

        self.workdir = Path(self.args.workdir).expanduser().resolve()
        self.workdir.mkdir(parents=True, exist_ok=True)
        if not self.workdir.is_dir():
            raise AgentError(f"workdir is not a directory: {self.workdir}")

        if self.chat is not None and not self.args.task:
            initial = WorkingContext()
            self.chat.boundary(
                initial, force=True, require_message=True,
                status="TASK REQUIRED — enter a task and press Enter; /quit ends.")
            self.task = initial.messages()[0]["content"].split("\n", 1)[1] if initial.messages() else ""
        else:
            self.task = " ".join(self.args.task).strip() if self.args.task else sys.stdin.read().strip()
        if not self.task:
            raise AgentError("task is empty")

        self.state_dir = Path(self.args.state_dir).expanduser().resolve() if self.args.state_dir else _default_state_dir(self.workdir)
        try:
            self.state_dir.relative_to(self.workdir)
        except ValueError:
            pass
        else:
            raise AgentError("--state-dir must be outside --workdir so the sandboxed worker cannot edit it")

        self.store = StateStore(self.state_dir, self.task, reset=self.args.reset_state, cold_restart=True)
        self.experiment = ExperimentRecorder(self.state_dir, reset=self.args.reset_state)
        self.project_map = None
        if getattr(self.args, "project_map", "off") == "on":
            try:
                map_indexer = PythonTreeSitterIndexer()
            except AgentError as exc:
                print("Project Map unavailable: " + str(exc)[:500], file=sys.stderr)
            else:
                self.project_map = ProjectMap(self.workdir, self.state_dir / "project_map.json",
                                         reset=self.args.reset_state, indexer=map_indexer)
        if self.args.verbose:
            state = self.store.load()
            print(f"[state] file: {self.store.state_path}", file=sys.stderr)
            print(f"[state] log:  {self.store.log_path}", file=sys.stderr)
            print(f"[state] starting at v{state.get('version', 0)}", file=sys.stderr)

        self.provider = ChatProvider(
            base_url=self.args.base_url,
            model=self.args.model,
            api_key=self.args.api_key,
            timeout=self.args.api_timeout,
            temperature=self.args.temperature,
            max_tokens=self.args.max_tokens,
        )
        if self.args.worker_context_budget is None:
            self.args.worker_context_budget = self.provider.discover_context_length()
            if self.args.verbose:
                print(
                    f"[worker-context] discovered active LM Studio context_length={self.args.worker_context_budget}",
                    file=sys.stderr,
                )

        self.budget = PromptBudget(self.args.worker_context_budget, self.args.history_context_high)
        self.operation_label = "shell/function" if self.functions is not None else "shell"

        network_note = (
            "Network permission is granted for this run. Each shell or gui_start action must "
            "still opt in with network=true to enable network access."
            if self.args.network
            else "Network access has NOT been granted. Always use network=false, including gui_start."
        )
        project_map_note = (
            "\n\nA controller-generated PROJECT MAP is supplied as a checkpoint navigation snapshot. "
            "It is refreshed at checkpoints and may lag subsequent edits. It never overrides the filesystem."
            if self.project_map is not None else ""
        )
        self.gui_runtime = None
        if bool(getattr(self.args, "gui", False)):
            try:
                self.gui_runtime = GuiRuntime(
                    self.workdir, self.state_dir,
                    network_allowed=bool(self.args.network),
                )
            except GuiError as exc:
                print("GUI unavailable: " + str(exc)[:500], file=sys.stderr)
        self.gui_enabled = self.gui_runtime is not None
        self.system_message = {
            "role": "system",
            "content": build_worker_system_prompt(gui_enabled=self.gui_enabled) + "\n\n" + network_note + project_map_note + ("\n\n" + EXPERT_PROMPT if self.expert else "") + (CHAT_PROMPT if self.chat else ""),
        }
        self.task_message = {"role": "user", "content": "TASK:\n" + self.task}
        if self.functions is not None:
            self.system_message["content"] += self.functions.prompt()

        self.recent = WorkingContext()
        self.project_state = self.store.get_project_state()
        self.state_prompt = project_state_message(self.project_state)
        self.map_prompt = None
        self.checkpoint_active = False
        self.handoff_prompt = handoff_message(self.store.load().get("recovery_checkpoint", {}).get("handoff", ""))
        self.pending_context_notice: dict[str, str] | None = None
        self.invalid_replies = 0
        self.consecutive_reasoning_recoveries = 0
        self.consecutive_loop_recoveries = 0
        self.live = (interactive_live or LiveConsoleRenderer(
            reasoning_loop_diagnostics=getattr(self.args, "reasoning_loop_diagnostics", False))) if getattr(self.args, "live", False) else None
        if self.live is not None:
            expert_status = ("off" if self.expert is None else
                             "enabled · network not granted" if not self.expert.network else
                             "enabled · call limit 0" if self.expert.max_calls == 0 else "available")
            self.live.start(model=self.provider.resolve_model(), task=self.task, max_steps=self.args.max_steps,
                       context=self.args.worker_context_budget, expert_status=expert_status)

        self.gui_observation: dict[str, object] | None = None
        self.overflow_recovered_generations: set[str] = set()
        self.operation_loops = OperationLoopDetector()
        self.operation_loop_resume = self.chat.resume_count if self.chat is not None else 0
        if self.chat is not None:
            self.chat.trim_history = self._trim_last_turns

        if self.chat is not None and self.args.task:
            self.chat.renderer.chat_message("USER", self.task)

    def run(self) -> int:
        with self.gui_runtime if self.gui_runtime is not None else nullcontext():
            for step in itertools.count(1):
                if self.args.max_steps != -1 and step > self.args.max_steps:
                    break
                self.step = step
                messages, context_stats = self._prepare_turn()
                turn = self._request_turn(messages)
                if turn is None:
                    continue
                outcome = self._process_turn(turn, context_stats)
                if outcome is not None:
                    return outcome
        raise AgentError(f"agent reached the maximum of {self.args.max_steps} steps without finishing")

    def _prepare_turn(self) -> tuple[list[dict[str, object]], dict[str, int]]:
        if self.args.verbose:
            print(f"[agent] step {self.step}/{self.args.max_steps}", file=sys.stderr)
        self.recent.current_step = self.step
        if self.chat is not None:
            self.chat.boundary(self.recent)
        self._sync_operation_loop_resume()
        # HIGH now requests a fresh checkpoint, never a destructive partial cut.
        # Keep all steps (including review attempts) until that checkpoint succeeds.
        history_overflow = self.recent.step_count() >= self.args.history_high

        state_snapshot = self.store.load()
        operation_count = int(state_snapshot.get("counters", {}).get("operation", 0) or 0)
        self.project_state = copy.deepcopy(state_snapshot.get("project_state", self.project_state))
        # Refresh startup Map before measuring the actual next request.
        if self.step == 1 and self.project_map is not None:
            self.project_map.refresh()
            self.map_prompt = self.project_map.message()
        periodic_prompt = None
        if review_due(self.project_state, operation_count=operation_count, every=self.args.project_review_every):
            periodic_prompt = {"role": "user", "content": (
                f"PERIODIC PROJECT STATE REVIEW\n{self.args.project_review_every} {self.operation_label} operations completed since the last review.\n"
                "This is an attention questionnaire, NOT a history checkpoint. Normal shell/finish actions are temporarily unavailable for this one review turn. "
                "If durable DESIGN/WORK/DEVIATION information changed, record it with project_update. "
                "If nothing durable needs recording, call project_review_skip. Do not call project_review_complete."
            )}
        history_reason = (
            f"recent history reached {self.args.history_high} complete Worker steps; "
            "review durable recovery information before the full history reset"
        )
        context_reason = (
            "provider-reported prompt usage reached the configured HIGH fraction of model context; "
            "review durable recovery information before the full history reset"
        )
        context_high = self.budget.needs_checkpoint()
        self.checkpoint_reason = (
            history_reason
            if history_overflow else (
                context_reason
                if context_high or self.checkpoint_active else None
            )
        )
        self.periodic_review_due = (
            not self.checkpoint_reason and periodic_prompt is not None
        )
        # Keep the early snapshots frozen throughout HIGH. Canonical updates
        # remain visible in History; refresh snapshots only after completion.
        self.checkpoint_active = bool(self.checkpoint_reason)

        messages = build_worker_messages(
            self.system_message, self.task_message, self.recent,
            project_state_prompt=self.state_prompt,
            project_map_message=self.map_prompt,
            context_notice=self.pending_context_notice,
            checkpoint_handoff=self.handoff_prompt,
            gui_observation=self.gui_observation,
        )
        if self.checkpoint_reason and self.project_state.get("initialized"):
            # The schema blocks shell during HIGH. Keep its exit protocol
            # after History too, especially after accepted project_update
            # results that otherwise look like permission to resume work.
            messages.append({"role": "user", "content": (
                "CURRENT RUNTIME PHASE: HIGH CHECKPOINT\nPROJECT CHECKPOINT REQUIRED\n"
                f"reason: {self.checkpoint_reason}\n"
                "Only project_update and project_review_complete are available; shell/finish are unavailable. "
                "A successful project_update does not complete this checkpoint or unlock normal work. "
                "Record only remaining durable changes; do not repeat already accepted updates. "
                "If there are no remaining durable changes, call project_review_complete now, "
                "with a short handoff for the next action. This saves recovery state; "
                "it does not finish the task or require all WORK items to be DONE. "
                "After Core accepts it, normal actions become available on the next turn."
            )})
        elif self.periodic_review_due:
            messages.append(periodic_prompt)
        self.experiment.record_context_preflight(step=self.step, phase="assembled", breakdown=self.budget.telemetry())
        self.pending_context_notice = None
        context_stats = self.recent.telemetry()
        if self.live is not None:
            self.live.step(self.step, self.args.max_steps)

        return messages, context_stats

    def _request_turn(self, messages: list[dict[str, object]]) -> ProviderTurn | None:
        # Cold overflow recovery clears History. Fail explicitly rather than lose/replay chat.
        interactive_evidence = self.chat is not None and any(m.get("content", "").startswith(
            "USER MESSAGE AT SAFE BOUNDARY:\n") for m in self.recent.messages())

        def before_generation():
            if self.chat is not None and self.chat.boundary(self.recent):
                raise RestartWorkerTurn()

        try:
            turn, self.consecutive_loop_recoveries = _worker_generation(
                self.provider, messages, budget=self.budget, mode=self.args.reasoning_loop_recovery,
                max_recoveries=self.args.max_reasoning_loop_recoveries,
                recoveries=self.consecutive_loop_recoveries, step=self.step, experiment=self.experiment, live=self.live,
                response_format=worker_response_format(
                    initialized=bool(self.project_state.get("initialized")),
                    checkpoint_required=bool(self.checkpoint_reason), periodic_review=self.periodic_review_due,
                    gui_enabled=self.gui_enabled, expert_enabled=self.expert is not None, interactive=self.chat is not None,
                    functions=self.functions.descriptions if self.functions is not None else None,
                ), before_generation=before_generation if self.chat is not None else None,
                reasoning_effort=getattr(self.args, "reasoning_effort", None),
                should_cancel=(lambda: self.chat.cancel_requested) if self.chat is not None else None,
            )
        except RestartWorkerTurn:
            # Cancellation must reach input even on the final allowed step.
            if self.chat is not None:
                self.chat.boundary(self.recent)
            return None
        except ReasoningLoopRecoveryExhausted as exc:
            if self.chat is None:
                raise AgentError(str(exc) + " Human escalation is unavailable in non-interactive mode.") from exc
            # Never convert an integrity failure into permission to continue.
            current = self.store.load()
            current_project = current['project_state']
            if current_project.get('initialized'):
                validate_checkpoint(current, self.task)
                validate_persisted_project_state(current_project)
            elif current_project != empty_project_state() or current.get('recovery_checkpoint') is not None:
                raise AgentError("invalid uninitialized Project State at reasoning-loop escalation")
            self.experiment.record_human_escalation(step=self.step, attempts=exc.attempts, resumed=False)
            self.chat.boundary(self.recent, force=True, require_message=True, status=(
                f"NEED USER — reasoning-loop recovery exhausted after {exc.attempts} attempts.\n"
                "The same TASK remains active; committed state and /work are preserved.\n"
                "Safe to type: provide new information or guidance; /quit ends."))
            # boundary returns only after genuine nonblank intervention, through
            # the existing chronological user_message path. No checkpoint/reset.
            self.consecutive_loop_recoveries = 0
            self.experiment.record_human_escalation(step=self.step, attempts=exc.attempts, resumed=True)
            return None
        except ProviderContextOverflow as exc:
            if interactive_evidence:
                raise AgentError("Context overflow with interactive evidence in Recent History; "
                                 "stopped without discarding or replaying user messages. "
                                 "/work and committed checkpoint preserved.") from exc
            # Exactly the strict startup recovery source, never the failed history.
            checkpoint = validate_checkpoint(self.store.load(), self.task)
            identity = checkpoint["sha256"]
            if identity in self.overflow_recovered_generations:
                raise AgentError("context overflow recovery exhausted: this committed checkpoint "
                                 "already recovered once; /work and factual ledger preserved") from exc
            self.overflow_recovered_generations.add(identity)
            self.store, self.project_state, self.state_prompt, self.handoff_prompt, self.map_prompt = _recover_checkpoint_baseline(
                self.state_dir, self.task, self.project_map)
            self.recent = WorkingContext()
            self.budget.reset()
            self.checkpoint_active = False
            self.invalid_replies = self.consecutive_reasoning_recoveries = self.consecutive_loop_recoveries = 0
            self.gui_observation = None
            if self.gui_runtime is not None:
                self.gui_runtime.close()
            self.pending_context_notice = context_notice_message(
                "Provider rejected a request for real context overflow. Restarted from the last committed "
                "checkpoint with empty Recent History. /work remains current; observe it before continuing.")
            self.experiment.record_context_recovery(step=self.step, checkpoint=identity, error=str(exc))
            if self.live is not None:
                self.live.prefix_changed("provider context overflow; cold checkpoint recovery")
            return None
        return turn

    def _process_turn(self, turn: ProviderTurn, context_stats: dict[str, int]) -> int | None:
        self.experiment.record_worker_turn(
            step=self.step,
            turn=turn,
            context_budget=self.args.worker_context_budget,
            context_stats=context_stats,
        )
        raw = turn.content
        if self.args.verbose and (
            turn.prompt_tokens is not None
            or turn.completion_tokens is not None
            or turn.reasoning_tokens is not None
        ):
            prompt_usage = str(turn.prompt_tokens) if turn.prompt_tokens is not None else "?"
            completion_usage = str(turn.completion_tokens) if turn.completion_tokens is not None else "?"
            reasoning_usage = str(turn.reasoning_tokens) if turn.reasoning_tokens is not None else "?"
            pct = (
                f" ({100.0 * turn.prompt_tokens / self.args.worker_context_budget:.1f}% context)"
                if turn.prompt_tokens is not None else ""
            )
            print(
                f"[worker-usage] prompt={prompt_usage}/{self.args.worker_context_budget}{pct} "
                f"completion={completion_usage} reasoning={reasoning_usage}",
                file=sys.stderr,
            )

        if self.live is not None:
            self.live.usage(prompt=turn.prompt_tokens, context_budget=self.args.worker_context_budget, completion=turn.completion_tokens, reasoning=turn.reasoning_tokens)

        if self.chat is not None and self.chat.boundary(self.recent):
            # Proposed action has not entered dispatch; regenerate with the intervention.
            return None

        if turn.reasoning_content.strip():
            self.recent.append({"role": "user", "content": (
                "WORKER REASONING (working hypotheses, not project truth):\n" + turn.reasoning_content
            )}, kind="reasoning")
        if not raw.strip():
            if (not self.args.no_reasoning_recovery and turn.reasoning_content.strip()
                    and self.consecutive_reasoning_recoveries < self.args.max_reasoning_recoveries):
                self.consecutive_reasoning_recoveries += 1
                self.recent.append({"role": "user", "content": (
                    "The previous worker attempt produced no final action"
                    + (f" (finish_reason={turn.finish_reason})" if turn.finish_reason else "")
                    + ". Its reasoning was retained for continuation. Continue and produce one valid JSON action."
                )})
                return None
            detail = f"finish_reason={turn.finish_reason}" if turn.finish_reason else "no finish_reason"
            if turn.reasoning_content.strip():
                detail += f", reasoning_chars={len(turn.reasoning_content)}"
            raise AgentError(f"provider returned empty assistant content ({detail})")

        try:
            action, kind, data = _validate_worker_action(
                turn, self.args, chat=self.chat, functions=self.functions, expert=self.expert, gui_enabled=self.gui_enabled)
            self.consecutive_reasoning_recoveries = 0
        except AgentError as exc:
            if self.live is not None:
                self.live.invalid(str(exc))
            self.invalid_replies += 1
            self.recent.append({"role": "assistant", "content": raw})
            self.recent.append({"role": "user", "content": (
                "INVALID ACTION: " + str(exc) + ". Reply again with exactly one valid JSON action and no prose."
            )})
            if self.invalid_replies >= 3:
                raise AgentError("model returned invalid actions three times in a row") from exc
            return None

        rejection = _project_action_rejection(
            kind, self.project_state, checkpoint_reason=self.checkpoint_reason,
            periodic_review_due=self.periodic_review_due, review_every=self.args.project_review_every,
            operation_label=self.operation_label)
        if rejection is not None:
            self.recent.append({"role": "assistant", "content": json.dumps(action, ensure_ascii=False)})
            self.recent.append({"role": "user", "content": rejection})
            return None

        return self._dispatch_action(action, kind, data)

    def _dispatch_action(self, action: dict[str, object], kind: str, data: dict[str, object]) -> int | None:
        # Dispatch admission is the last safe boundary. Everything below through result
        # recording/restoration is one indivisible operation; a later signal reserves the next boundary.
        if self.chat is not None and self.chat.boundary(self.recent):
            return None
        assistant_message = {"role": "assistant", "content": json.dumps(action, ensure_ascii=False)}
        self.recent.append(assistant_message)
        self.experiment.record_worker_action(step=self.step, kind=kind, data=data)
        if self.live is not None:
            self.live.action(kind, data)
        if kind in {"message", "wait_for_user"}:
            return self._chat_action(kind, data)
        if kind in GUI_ACTIONS:
            self.operation_loops.clear()
            return self._gui_action(kind, data)
        if kind == "shell":
            return self._shell_action(data, assistant_message)
        handlers = {
            "project_init": self._initialize_project,
            "project_update": self._update_project,
            "project_review_skip": self._skip_project_review,
            "project_review_complete": self._complete_project_review,
            "call_function": self._call_function,
            "ask_expert": self._ask_expert,
            "finish": self._finish,
        }
        return handlers[kind](data)

    def _initialize_project(self, data: dict[str, object]) -> int | None:
        try:
            self.project_state = self.store.initialize_project(data, step=self.step)
        except AgentError as exc:
            if self.live is not None:
                self.live.invalid("PROJECT INIT REJECTED: " + str(exc))
            self.invalid_replies += 1
            self.recent.append({"role": "user", "content": (
                "PROJECT INIT REJECTED: " + str(exc) + ". Revise the initial plan and call project_init again."
            )})
            if self.invalid_replies >= 3:
                raise AgentError("Project State initialization was rejected three times in a row") from exc
            return None
        self.invalid_replies = 0
        self.consecutive_loop_recoveries = 0
        self.recent.append({"role": "user", "content": "PROJECT STATE INITIALIZED:\n" + json.dumps(self.project_state, ensure_ascii=False)})
        self.state_prompt = project_state_message(self.project_state)
        if self.checkpoint_reason:
            self.pending_context_notice = _reset_checkpoint_history(
                self.state_dir, self.recent, self.budget, self.experiment, step=self.step,
                reason="initial checkpoint established; full history reset")
            self.checkpoint_active = False
        return None

    def _update_project(self, data: dict[str, object]) -> int | None:
        try:
            self.project_state = self.store.update_project(data["changes"], step=self.step)
        except AgentError as exc:
            if self.live is not None:
                self.live.invalid("PROJECT UPDATE REJECTED: " + str(exc))
            self.invalid_replies += 1
            self.recent.append({"role": "user", "content": (
                "PROJECT UPDATE REJECTED: " + str(exc) + ". Correct the Project State mutation; normal work has not been changed by this rejected update."
            )})
            if self.invalid_replies >= 3:
                raise AgentError("Project State update was rejected three times in a row") from exc
            return None
        self.invalid_replies = 0
        if self.periodic_review_due:
            self.project_state = self.store.acknowledge_periodic_review(step=self.step)
        self.consecutive_loop_recoveries = 0
        self.recent.append({"role": "user", "content": "PROJECT STATE UPDATED:\n" + json.dumps(self.project_state, ensure_ascii=False)})
        return None

    def _skip_project_review(self, data: dict[str, object]) -> int | None:
        if not self.periodic_review_due:
            self.recent.append({"role": "user", "content": "PERIODIC REVIEW SKIP REJECTED: no periodic Project State review is due."})
            return None
        self.project_state = self.store.acknowledge_periodic_review(step=self.step)
        self.consecutive_loop_recoveries = 0
        self.recent.append({"role": "user", "content": "PERIODIC PROJECT STATE REVIEW COMPLETE. No durable update recorded."})
        self.invalid_replies = 0
        return None

    def _complete_project_review(self, data: dict[str, object]) -> int | None:
        try:
            self.project_state = self.store.complete_project_review(
                step=self.step, note=data.get("note", ""), handoff=data["handoff"])
        except AgentError as exc:
            if self.live is not None:
                self.live.invalid("PROJECT REVIEW REJECTED: " + str(exc))
            self.invalid_replies += 1
            self.recent.append({"role": "user", "content": (
                "PROJECT REVIEW REJECTED: " + str(exc) + ". Correct the Project State and complete the checkpoint again."
            )})
            if self.invalid_replies >= 3:
                raise AgentError("Project State review was rejected three times in a row") from exc
            return None
        self.invalid_replies = 0
        self.consecutive_loop_recoveries = 0
        self.handoff_prompt = handoff_message(data["handoff"])
        self.recent.append({"role": "user", "content": "PROJECT REVIEW COMPLETE. Durable recovery checkpoint accepted."})
        # Only a Core-required HIGH checkpoint may refresh the frozen authoritative prefix.
        if self.project_map is not None:
            self.project_map.refresh()
            self.map_prompt = self.project_map.message()
        self.state_prompt = project_state_message(self.project_state)
        self.checkpoint_active = False
        if self.live is not None:
            self.live.prefix_changed("checkpoint snapshot refresh")
        if self.checkpoint_reason:
            self.pending_context_notice = _reset_checkpoint_history(
                self.state_dir, self.recent, self.budget, self.experiment, step=self.step,
                reason="checkpoint completed; full history reset")
            self.checkpoint_active = False
        return None

    def _call_function(self, data: dict[str, object]) -> int | None:
        result_payload = self.functions.call(data, self.args.output_limit)
        # Use the existing factual ledger format; do not serialize Python state.
        ledger_payload = {
            "command": "call_function " + (result_payload["name"] or "<invalid>"),
            "stdout": json.dumps(result_payload, ensure_ascii=False),
            "error": result_payload.get("error", ""),
            "output_withheld": result_payload.get("output_withheld", False),
            "output_limit_chars": result_payload.get("output_limit_chars"),
        }
        op_record = self.store.record_operation(ledger_payload)
        self.experiment.record_operation_telemetry(step=self.step, op_id=op_record["id"], result=ledger_payload)
        if self.live is not None:
            self.live.function_result(op_record["id"], result_payload)
        self.recent.append({"role": "user", "content": f"FUNCTION RESULT ({op_record['id']}):\n" +
                       json.dumps(result_payload, ensure_ascii=False)},
                      kind="function_result", op_id=op_record["id"])
        self.consecutive_loop_recoveries = 0
        self.invalid_replies = 0
        self._check_operation_loop("call_function", data, result_payload)
        return None

    def _ask_expert(self, data: dict[str, object]) -> int | None:
        self.operation_loops.clear()
        def expert_telemetry(event):
            self.experiment.record_expert_call(step=self.step, event=event)
            if self.live is not None:
                self.live.expert_event(event)
        result_payload = self.expert.ask(
            data["question"], data["context"],
            telemetry=expert_telemetry,
        )
        if self.live is not None:
            self.live.expert_result(result_payload)
        self.recent.append({"role": "user", "content": "EXPERT RESULT:\n" +
                       json.dumps(result_payload, ensure_ascii=False)}, kind="expert_result")
        self.invalid_replies = 0
        return None

    def _finish(self, data: dict[str, object]) -> int | None:
        unfinished = [item for item in self.project_state.get("work", [])
                      if item.get("status") not in {"DONE", "SUPERSEDED"}]
        if unfinished:
            self.recent.append({"role": "user", "content": (
                "FINISH REJECTED: Project State still contains unfinished work: " +
                ", ".join(f"{item.get('id')}={item.get('status')}" for item in unfinished) +
                ". Update work status before finishing."
            )})
            return None
        self.store.mark_finished(data["summary"])
        if self.chat is not None:
            self.chat.renderer.chat_message("P.A.V.L.U.S.H.A.", data["summary"])
        elif self.live is not None:
            self.live.complete(data["summary"])
        else:
            print(data["summary"])
        return 0

    def _chat_action(self, kind: str, data: dict[str, object]) -> None:
        self.chat.renderer.chat_message("P.A.V.L.U.S.H.A.", data["text"])
        # Like every other action, close the assistant action with factual tool evidence.
        # Ending the prompt on assistant can be interpreted as assistant-prefill/EOS.
        self.recent.append({"role": "user", "content": "MESSAGE RESULT: user-facing text displayed."},
                      kind="message_result")
        self.invalid_replies = 0
        if kind == "wait_for_user":
            self.chat.boundary(self.recent, force=True)
        return None

    def _gui_action(self, kind: str, data: dict[str, object]) -> None:
        if self.gui_runtime is None:
            raise AgentError("internal GUI action accepted while --gui is disabled")
        result_payload, observation = self.gui_runtime.execute(kind, data)
        self.gui_observation = observation.message() if observation is not None else None
        if not result_payload.get("error") and result_payload.get("state") == "alive":
            self.consecutive_loop_recoveries = 0
        if self.live is not None:
            self.live.gui_result(result_payload)
        self.recent.append({
            "role": "user",
            "content": "GUI RESULT:\n" + json.dumps(result_payload, ensure_ascii=False),
        }, kind="gui_result")
        self.invalid_replies = 0
        return None

    def _shell_action(self, data: dict[str, object], assistant_message: dict[str, str]) -> None:
        result_payload, op_record, release_worker = _execute_shell_action(
            data, args=self.args, provider=self.provider, store=self.store, workdir=self.workdir, task=self.task, step=self.step)
        if not data["network"] or self.args.network:
            self.consecutive_loop_recoveries = 0

        if release_worker:
            self.store, self.project_state, self.state_prompt, self.handoff_prompt, self.map_prompt = _recover_checkpoint_baseline(
                self.state_dir, self.task, self.project_map)
            _archive_history(self.state_dir, self.recent.step_records())
            self.recent = WorkingContext()
            self.recent.current_step = self.step
            self.recent.append(assistant_message)
            self.budget.reset()
            self.checkpoint_active = False
            self.invalid_replies = self.consecutive_reasoning_recoveries = self.consecutive_loop_recoveries = 0
            self.gui_observation = None
            if self.gui_runtime is not None:
                self.gui_runtime.close()
            self.pending_context_notice = context_notice_message(
                "Worker restored after intentional release for bounded shell execution. "
                "Resumed from the fresh committed checkpoint with empty previous Recent History. "
                "/work remains current; the factual shell result follows.")
            self.experiment.record_prefix_changed(step=self.step, reason="Worker release; cold checkpoint recovery")
            if self.live is not None:
                self.live.prefix_changed("Worker restored; cold checkpoint recovery")
        self.experiment.record_operation_telemetry(step=self.step, op_id=op_record["id"], result=result_payload)
        if self.live is not None:
            self.live.operation(op_record["id"], result_payload)
        if self.args.verbose:
            print(
                f"[state] recorded {op_record['id']} exit={op_record.get('exit_code')} "
                f"timeout={str(op_record.get('timed_out', False)).lower()}",
                file=sys.stderr,
            )

        result_message = {
            "role": "user",
            "content": f"SHELL RESULT ({op_record['id']}):\n" + json.dumps(result_payload, ensure_ascii=False),
        }
        self.recent.append(result_message, kind="shell_result", op_id=op_record["id"])

        self.invalid_replies = 0
        self._check_operation_loop("shell", data, result_payload)

    def _sync_operation_loop_resume(self):
        if self.chat is not None and self.chat.resume_count != self.operation_loop_resume:
            self.operation_loops.clear()
            self.operation_loop_resume = self.chat.resume_count

    def _trim_last_turns(self, count: int) -> None:
        records = self.recent.last_turn_records(count)
        steps = list(dict.fromkeys(record['step'] for record in records))
        # Materialize current durable facts before removing their chronological update evidence.
        state_prompt = project_state_message(self.project_state)
        _archive_history(self.state_dir, records)
        self.experiment.record_context_trim(step=self.step, steps=steps)
        self.recent.trim_last_turns(count)
        self.state_prompt = state_prompt
        self.gui_observation = None
        self.budget.reset()
        self.operation_loops.clear()
        self.recent.append({'role': 'user', 'content': (
            'CONTEXT HISTORY TRIMMED BY USER: completed turns '
            + ', '.join(map(str, steps)) + ' were removed from active context. '
            'Project State, files and executed tool effects were NOT rolled back. '
            'Use current Project State and inspect current files before relying on forgotten actions.')},
            kind='context_trim')

    def _check_operation_loop(self, kind, action, result):
        self._sync_operation_loop_resume()
        match = self.operation_loops.feed(kind, action, result, step=self.step)
        if match is None:
            return
        notice = (
            f"OPERATION LOOP DETECTED: cycle of {match.cycle_length} action/result pairs "
            f"repeated 3 times at steps {', '.join(map(str, match.steps))}. "
            "Actions have already executed and their results were recorded."
        )
        self.experiment.record_operation_loop(step=self.step, cycle_length=match.cycle_length,
                                             steps=match.steps)
        self.recent.append({"role": "user", "content": notice}, kind="operation_loop")
        self.operation_loops.clear()
        if self.chat is None:
            raise AgentError(notice + " Human escalation requires --interactive; stopped before the next model call.")
        self.chat.boundary(self.recent, force=True, status=(notice +
            "\nPAUSED — safe to type. Message + Enter; empty Enter resumes; /quit ends."))
        self.operation_loop_resume = self.chat.resume_count
