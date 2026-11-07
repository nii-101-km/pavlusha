"""Bare event-driven turn loop. Project policy belongs to future extensions."""
from dataclasses import asdict
import copy
import json
import sys
import threading
from collections import deque

from .config import SYSTEM_PROMPT
from .core import AgentError, ProcessCleanupError, _extract_json_object
from .extensions import Event, EventHost, Outcome, InvocationContext, validate_controls, validate_observations
from .model_backend import ProviderContextOverflow, WorkerStreamInterrupted
from .session import Session

SERVICE = '''EXPERIMENTAL SESSION
Use only actions in this request's response schema and follow the instructions
provided by the active extensions. Context resets retain the original task and
work files; inspect current work files after a reset. Instrument execution resources are managed by the kernel.
'''


class Interaction:
    """Narrow module interface for pause, input and explicit continuation."""
    def __init__(self, runtime):
        self.__request_pause = runtime.request_pause
        self.__read_user_input = runtime.read_user_input
        self.__resume = runtime.resume

    def request_pause(self, *, urgent=False):
        return self.__request_pause(urgent=urgent)

    def read_user_input(self, prompt='', *, require_message=False):
        return self.__read_user_input(prompt, require_message=require_message)

    def resume(self, message):
        return self.__resume(message)


class Runtime:
    def __init__(self, provider, *, task, session_dir, extensions, max_steps=40,
                 history_high=200, context_capacity=32768, context_fraction=.85,
                 output_limit=24000, reasoning_effort='low', live=False, user_input=None, interaction_ui=None,
                 renderer=None):
        self.provider = provider
        self.task = task
        self.session = Session(session_dir, task)
        if self.session.state.get('awaiting_input'):
            if type(user_input) is not str or not user_input.strip():
                raise AgentError('session is paused and requires user_input to resume')
            self.session.state['pending_user_reply'] = user_input
            self.session.state.pop('awaiting_input')
            self.session.save()
        elif user_input is not None:
            raise AgentError('user_input requires a paused session')
        self.host = EventHost(extensions)
        self.interaction_ui = interaction_ui
        self._key_lock = threading.RLock()
        self._keys = deque()
        self._pause_request = None
        self._pause_key = None
        self._current_key = None
        self._interactive_paused = False
        self._resumed = False
        self._resume_value = None
        for module in self.host.extensions:
            bind = getattr(module, 'bind_interaction', None)
            if callable(bind):
                bind(Interaction(self))
            bind_journal = getattr(module, 'bind_event_journal', None)
            if callable(bind_journal):
                bind_journal(self.session.completed_turns)
        self.max_steps = max_steps
        self.history_high = history_high
        self.context_capacity = context_capacity
        self.context_fraction = context_fraction
        self.output_limit = output_limit
        self.reasoning_effort = reasoning_effort
        self.live = live
        self.renderer = renderer
        self.measured_tokens = None
        self.overflow_without_progress = False

    def post_key(self, key):
        """Thread-safe entry point for terminal or other keyboard sources."""
        if type(key) is not str or not key:
            raise AgentError('key must be a nonempty string')
        with self._key_lock:
            self._keys.append(key)

    def request_pause(self, *, urgent=False):
        if type(urgent) is not bool:
            raise AgentError('urgent must be boolean')
        if self.interaction_ui is None:
            raise AgentError('live interaction is unavailable')
        with self._key_lock:
            if self._interactive_paused:
                return
            if self._pause_request != 'urgent':
                self._pause_request = 'urgent' if urgent else 'normal'
                self._pause_key = self._current_key

    def read_user_input(self, prompt='', *, require_message=False):
        if not self._interactive_paused or self.interaction_ui is None or self._resumed:
            raise AgentError('user input is available only during an active pause')
        if type(prompt) is not str or type(require_message) is not bool:
            raise AgentError('invalid input request')
        return self.interaction_ui.read_message(prompt, require_message=require_message)

    def resume(self, message):
        if not self._interactive_paused or self._resumed:
            raise AgentError('resume requires an active pause')
        if message is not None and type(message) is not str:
            raise AgentError('resume requires a text message or EOF')
        self._resume_value = message
        self._resumed = True

    def _poll_keys(self):
        with self._key_lock:
            while self._keys:
                key = self._keys.popleft()
                self._current_key = key
                try:
                    self.emit('key_pressed', key=key)
                finally:
                    self._current_key = None

    def _stream_cancelled(self):
        self._poll_keys()
        return self._pause_request == 'urgent'

    def _pause_at_boundary(self, reason='', *, require_message=False):
        """Wait in-process; modules handle input and explicitly request continuation."""
        key = self._pause_key
        self._pause_request = None
        self._pause_key = None
        self._interactive_paused = True
        self._resumed = False
        self._resume_value = None
        self.session.state['status'] = 'paused'
        self.session.state['awaiting_input'] = reason or 'user pause'
        self.session.save()
        try:
            if self.renderer is not None:
                self.renderer.chat_status('Пауза: ожидание ввода пользователя.')
            self.emit('paused', reason=reason, require_message=require_message, key=key)
            if not self._resumed:
                raise AgentError('no module resumed the interactive pause')
            message = self._resume_value
            if message is not None and require_message and not message.strip():
                raise AgentError('a nonempty user reply is required')
            self.session.state.pop('awaiting_input', None)
            if message is None:
                self.session.state['status'] = 'stopped'
                self.session.save()
                return 'Session ended by user.'
            if message.strip():
                old = self.session.state.get('pending_user_reply')
                self.session.state['pending_user_reply'] = old + '\n' + message if old else message
            self.session.state['status'] = 'running'
            self.session.save()
            return None
        finally:
            self._interactive_paused = False

    def _handle_user_pause(self):
        if self._pause_request:
            return self._pause_at_boundary()
        return None

    def event(self, kind, **data):
        state = self.session.state
        turn_id = state['turn_id'] + (1 if kind == 'reasoning_fragment' else 0)
        return Event(kind, state['session_id'], turn_id, state['context_epoch'], data)

    def emit(self, kind, **data):
        event = self.event(kind, **data)
        # Streaming observations are transient: avoid a synchronous durable write
        # for every fragment. Completed turns still persist the full response.
        if kind not in ('reasoning_fragment', 'key_pressed'):
            self.session.journal('lifecycle', event=asdict(event))
        self.host.emit(event)

    def reset(self, reason, *, mandatory=False):
        self.emit('before_reset', reason=reason, mandatory=mandatory,
                  history=copy.deepcopy(self.session.state['history']))
        if any(c.kind in ('stop', 'pause') for c in self.host.controls):
            return False
        if not self.host.reset_ready():
            if mandatory:
                raise AgentError('context overflow: a module deferred reset; stopped without discarding history')
            return False
        self.session.reset(reason)
        self.host.take_controls('reset')
        self.measured_tokens = None
        return True

    def run(self):
        if getattr(self, '_ran', False):
            raise AgentError('this runtime already ran; create a new runtime to resume the session')
        self._ran = True
        failure = None
        reason = 'error'
        try:
            if self.interaction_ui is not None:
                self.interaction_ui.start(self.post_key)
            self.session.state['status'] = 'running'
            self.session.save()
            self.emit('session_started', task=self.task, resumed=self.session.resumed,
                      session_dir=str(self.session.directory), last_turn_id=self.session.state['turn_id'])
            result = self._loop()
            reason = self.session.state.get('status', 'stopped')
            if reason == 'running':
                reason = 'stopped'
            return result
        except BaseException as exc:
            failure = exc
            reason = 'cancelled' if isinstance(exc, KeyboardInterrupt) else 'error'
            raise
        finally:
            event = self.event('session_stopped', reason=reason,
                               error=type(failure).__name__ if failure else None)
            persistence_error = None
            try:
                # A committed finish stays terminal even if a later observer fails.
                # The stop event still records the failure for diagnostics.
                if self.session.state['status'] != 'finished':
                    self.session.state['status'] = reason
                self.session.save()
                self.session.journal('lifecycle', event=asdict(event))
            except BaseException as exc:
                persistence_error = exc
            try:
                try:
                    self.host.stop(event)
                finally:
                    if self.interaction_ui is not None:
                        self.interaction_ui.close()
            except BaseException as cleanup:
                if failure is not None:
                    raise AgentError(f'{type(failure).__name__}: {failure}; cleanup failed: {cleanup}') from failure
                raise
            if persistence_error is not None:
                raise AgentError(f'session stop persistence failed: {persistence_error}') from persistence_error

    def _loop(self):
        for _ in range(self.max_steps):
            self._poll_keys()
            terminal = self._handle_user_pause()
            if terminal is not None:
                return terminal
            terminal = self.apply_terminal_controls()
            if terminal is not None:
                return terminal
            state = self.session.state
            if self.host.reset_requested():
                self.reset('module_requested')
            elif len(state['history']) >= self.history_high:
                self.reset('history_high')
            elif self.measured_tokens is not None and self.measured_tokens >= self.context_capacity * self.context_fraction:
                self.reset('context_high')
            terminal = self.apply_terminal_controls()
            if terminal is not None:
                return terminal
            state = self.session.state
            turn_id = state['turn_id'] + 1
            self.emit('prepare_turn', next_turn_id=turn_id,
                      first_in_context=not state['history'], reset_reason=state['reset_reason'])
            terminal = self.apply_terminal_controls()
            if terminal is not None:
                return terminal
            self._poll_keys()
            terminal = self._handle_user_pause()
            if terminal is not None:
                return terminal
            view = self.host.snapshot()
            # Keep the prefix stable throughout an epoch so llama.cpp can reuse it.
            # Turn IDs are still available to extensions and in the session journal.
            service = SERVICE + f'\nContext epoch: {state["context_epoch"]}.'
            if state['context_epoch']:
                service += '\nCONTEXT RESET: previous history is archived.'
            system_prompt = '\n\n'.join((SYSTEM_PROMPT, *view.system_instructions))
            messages = [{'role': 'system', 'content': system_prompt},
                        {'role': 'user', 'content': 'TASK:\n' + self.task},
                        {'role': 'user', 'content': service}, *copy.deepcopy(view.prefix)]
            for record in self.host.model_history(state['history']):
                messages.extend(record['messages'])
            if state.get('pending_user_reply') is not None:
                messages.append({'role': 'user', 'content': 'USER REPLY:\n' + state['pending_user_reply']})
            messages.extend(copy.deepcopy(view.messages))
            response_format = view.response_format()
            request = {'messages': copy.deepcopy(messages), 'response_format': copy.deepcopy(response_format)}
            self.session.journal('request', turn_id=turn_id, context_epoch=state['context_epoch'], **request)
            if self.renderer is not None:
                self.renderer.step(turn_id, self.max_steps)
                self.renderer.begin_worker()
            reply, action, result, fatal = None, None, None, None
            release = None
            release_entered = False
            outcome = None
            status, stop = 'completed', False
            try:
                self.host.consume_observations(view)
                fragment_index = 0
                interrupted = False

                def on_delta(channel, text):
                    nonlocal fragment_index, interrupted
                    if self.interaction_ui is not None and self._stream_cancelled():
                        interrupted = True
                        return False
                    if self.live:
                        with (self.session.directory / 'generation.jsonl').open('a') as output:
                            output.write(json.dumps({'turn_id': turn_id, 'channel': channel, 'text': text},
                                                    ensure_ascii=False) + '\n')
                    if self.renderer is not None and channel == 'reasoning':
                        self.renderer.delta(channel, text)
                    if channel == 'reasoning' and text:
                        fragment_index += 1
                        self.emit('reasoning_fragment', text=text, fragment_index=fragment_index)
                    if any(c.kind in ('interrupt', 'stop', 'pause') for c in self.host.controls):
                        interrupted = True
                        return False

                options = {'response_format': response_format, 'reasoning_effort': self.reasoning_effort,
                           'on_delta': on_delta}
                if self.interaction_ui is not None:
                    options['should_cancel'] = lambda: self._stream_cancelled()
                reply = self.provider.worker_completion(messages, **options)
                if self.interaction_ui is not None:
                    self._poll_keys()
                    interrupted = interrupted or self._pause_request == 'urgent'
                # Providers must honor False; even a nonconforming provider cannot execute its reply.
                if interrupted:
                    raise WorkerStreamInterrupted(reply)
            except WorkerStreamInterrupted as exc:
                requests = self.host.take_controls('interrupt')
                user_interrupt = self._pause_request == 'urgent'
                self._complete(turn_id, request, None if user_interrupt else exc.turn, None,
                    {'error': 'generation_interrupted', 'reasons': [c.reason for c in requests]},
                    'generation_interrupted', False)
                if self.renderer is not None:
                    self.renderer.chat_status('Генерация прервана; действие не выполнено.')
                terminal = self._handle_user_pause()
                if terminal is not None:
                    return terminal
                continue
            except ProviderContextOverflow as exc:
                # No action was obtained or executed; publish the factual failed attempt.
                status, result = 'provider_error', {'error': 'context_overflow'}
                self._complete(turn_id, request, reply, action, result, status, False)
                if self.overflow_without_progress or not state['history']:
                    raise AgentError('context overflow with no removable progress; refusing an endless reset loop') from exc
                self.overflow_without_progress = True
                self.reset('provider_overflow', mandatory=True)
                continue
            except Exception as exc:
                self._complete(turn_id, request, reply, action, {'error': type(exc).__name__}, 'provider_error', False)
                raise
            self.overflow_without_progress = False
            self.measured_tokens = reply.prompt_tokens
            if self.renderer is not None:
                self.renderer.usage(prompt=reply.prompt_tokens, context_budget=self.context_capacity,
                                    completion=reply.completion_tokens, reasoning=reply.reasoning_tokens)
            try:
                if reply.finish_reason == 'length':
                    raise AgentError('truncated model response; no action executed')
                action = _extract_json_object(reply.content)
                if type(action) is not dict:
                    raise AgentError('model response must be an object')
                # Validate first. A rejected action must never become pending/executable.
                from jsonschema import Draft202012Validator
                Draft202012Validator(response_format['json_schema']['schema']).validate(action)
            except Exception as exc:
                status = 'rejected'
                result = {'error': 'invalid_action', 'detail': str(exc)[:1000]}
            else:
                # Serialize key admission with the final pre-dispatch check. Once
                # begin_action succeeds, both pause modes wait for the tool result.
                with self._key_lock:
                    self._poll_keys()
                    user_interrupt = self._pause_request == 'urgent'
                    if not user_interrupt:
                        self.session.begin_action(action)
                if user_interrupt:
                    self._complete(turn_id, request, None, None,
                        {'error': 'generation_interrupted', 'reasons': ['user urgent pause']},
                        'generation_interrupted', False)
                    terminal = self._handle_user_pause()
                    if terminal is not None:
                        return terminal
                    continue
                if self.renderer is not None:
                    self.renderer.action(action['action'], action)
                try:
                    tool = view.tool_for(action)
                    if tool.execution.release_model:
                        release = self.provider.released_worker(restore_guard=tool.execution.restore_guard)
                        release.__enter__()
                        release_entered = True
                    context = InvocationContext(state['session_id'], turn_id, state['context_epoch'], asdict(reply))
                    if tool.accepts_tool_calls:
                        # The snapshot and outer pending action stay in force for the whole call.
                        from dataclasses import replace
                        def call_tool(nested):
                            if type(nested) is not dict or type(nested.get('action')) is not str:
                                raise AgentError('invalid programmatic tool request')
                            target = view.tool_for(nested)
                            if (not target.programmatic or target.accepts_tool_calls or target.execution.release_model
                                    or view.tool_owners[target.name] == view.tool_owners[tool.name]):
                                raise AgentError('tool does not allow programmatic calls')
                            self.session.journal('programmatic_call_started', turn_id=turn_id, action=nested)
                            try:
                                nested_outcome = view.invoke(nested, replace(context, call_tool=None))
                                if (not isinstance(nested_outcome, Outcome) or type(nested_outcome.result) is not dict
                                        or nested_outcome.controls or nested_outcome.observations):
                                    raise AgentError('programmatic tool returned unsupported outcome; effects may have occurred')
                                raw_result = json.dumps(nested_outcome.result, allow_nan=False, ensure_ascii=False)
                                if len(raw_result) > self.output_limit:
                                    raise AgentError('programmatic result exceeds output limit; effects may have occurred')
                                self.session.journal('programmatic_call_completed', turn_id=turn_id,
                                                     action=nested, result=nested_outcome.result)
                                return nested_outcome.result
                            except BaseException as exc:
                                self.session.journal('programmatic_call_failed', turn_id=turn_id,
                                                     action=nested, error=type(exc).__name__)
                                raise
                        context = replace(context, call_tool=call_tool)
                    outcome = view.invoke(action, context)
                    if not isinstance(outcome, Outcome):
                        raise AgentError('function must return Outcome')
                    if type(outcome.result) is not dict:
                        raise AgentError('tool result must be an object')
                    validate_controls(outcome.controls)
                    validate_observations(outcome.observations)
                    raw = json.dumps(outcome.result, allow_nan=False, ensure_ascii=False)
                    if len(raw) > self.output_limit:
                        result = {'error': 'output_limit_exceeded', 'output_withheld': True,
                                  'output_chars': len(raw), 'effects_may_have_occurred': True}
                    else:
                        result = outcome.result
                        stop = any(c.kind == 'stop' and c.finished for c in outcome.controls)
                except (KeyboardInterrupt, SystemExit, ProcessCleanupError) as exc:
                    status, fatal = 'interrupted', exc
                    result = {'error': type(exc).__name__, 'effects_may_have_occurred': True}
                except Exception as exc:
                    status = 'execution_error'
                    result = {'error': 'execution_error', 'detail': str(exc)[:1000],
                              'effects_may_have_occurred': True}
            try:
                self._complete(turn_id, request, reply, action, result, status, stop, preserve_pending=fatal is not None,
                    outcome=outcome if status == 'completed' and not result.get('output_withheld') else None)
            finally:
                if release_entered:
                    # The result is durable before restoration; a failed guard/restore stops the run.
                    error = fatal or sys.exc_info()[1]
                    release.__exit__(type(error) if error else None, error,
                                     error.__traceback__ if error else None)
            if outcome is not None and status == 'completed' and not result.get('output_withheld'):
                self.host.add_outcome(action['action'], outcome, view)
            if fatal is not None:
                raise fatal
            if self.renderer is not None:
                self.renderer.turn_result(turn_id, action, result)
            elif self.live:
                print(json.dumps({'turn': turn_id, 'action': action, 'result': result}, ensure_ascii=False), flush=True)
            # A completed finish is terminal; other tools reach the safe pause boundary.
            if not stop:
                self._poll_keys()
                terminal = self._handle_user_pause()
                if terminal is not None:
                    return terminal
            terminal = self.apply_terminal_controls()
            if terminal is not None:
                return terminal
        raise AgentError(f'bare agent reached maximum of {self.max_steps} turns')

    def apply_terminal_controls(self):
        stops = self.host.take_controls('stop')
        pauses = self.host.take_controls('pause')
        if stops:
            self.session.journal('control_applied', controls=[asdict(c) for c in stops])
            control = next((c for c in stops if not c.finished), stops[0])
            self.session.state['status'] = 'finished' if control.finished else 'stopped'
            self.session.save()
            return control.reason
        if pauses:
            if len(pauses) != 1:
                raise AgentError('conflicting pause requests')
            self.session.journal('control_applied', controls=[asdict(c) for c in pauses])
            if self.interaction_ui is not None:
                return self._pause_at_boundary(pauses[0].reason, require_message=True)
            self.session.state['status'] = 'paused'
            self.session.state['awaiting_input'] = pauses[0].reason
            self.session.save()
            return pauses[0].reason
        return None

    def _complete(self, turn_id, request, reply, action, result, status, stop, *, preserve_pending=False, outcome=None):
        turn = {'turn_id': turn_id, 'context_epoch': self.session.state['context_epoch'],
                'request': request, 'response': asdict(reply) if reply is not None else None,
                'action': action, 'result': result, 'status': status, 'stop': stop,
                'controls': [asdict(c) for c in outcome.controls] if outcome else [],
                'observations': [asdict(o) for o in outcome.observations] if outcome else []}
        messages = []
        if self.session.state.get('pending_user_reply') is not None:
            messages.append({'role': 'user', 'content': 'USER REPLY:\n' + self.session.state.pop('pending_user_reply')})
        if reply is not None:
            if reply.reasoning_content:
                messages.append({'role': 'user', 'content': 'WORKER REASONING (working hypotheses):\n' + reply.reasoning_content})
            messages.append({'role': 'assistant', 'content': reply.content})
        messages.append({'role': 'user', 'content': 'RESULT:\n' + json.dumps(result, ensure_ascii=False)})
        if outcome:
            for observation in outcome.observations:
                if observation.lifetime == 'history':
                    messages.append({'role': 'user', 'content': observation.parts()})
        self.session.complete(turn, messages, preserve_pending=preserve_pending)
        self.emit('turn_completed', turn=turn)
