"""Experimental event contract. No Project State, Map or legacy registries."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
import json
from typing import Callable, Literal, Protocol

from jsonschema import Draft202012Validator

from .core import AgentError

MODULE_API_VERSION = 3

EventKind = Literal['session_started', 'prepare_turn', 'reasoning_fragment',
                    'turn_completed', 'before_reset', 'session_stopped', 'key_pressed', 'paused']


@dataclass(frozen=True)
class Event:
    kind: EventKind
    session_id: str
    turn_id: int
    context_epoch: int
    data: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Control:
    """A requested transition, executed only by the kernel at a safe boundary."""
    kind: Literal['reset', 'interrupt', 'stop', 'pause']
    reason: str
    finished: bool = False


@dataclass(frozen=True)
class Observation:
    """Owned model observation; next_request is consumed only when actually visible."""
    text: str = ''
    image: str = ''  # embedded data:image/png|jpeg;base64; never a host path or URL
    lifetime: Literal['next_request', 'until_replaced'] = 'next_request'

    def parts(self):
        parts = []
        if self.text:
            parts.append({'type': 'text', 'text': self.text})
        if self.image:
            parts.append({'type': 'image_url', 'image_url': {'url': self.image}})
        return parts


@dataclass(frozen=True)
class InvocationContext:
    session_id: str
    turn_id: int
    context_epoch: int
    response: dict


@dataclass(frozen=True)
class ExecutionPolicy:
    release_model: bool = False
    restore_guard: Callable[[], None] | None = None


@dataclass(frozen=True)
class Outcome:
    result: dict
    controls: tuple[Control, ...] = ()
    observations: tuple[Observation, ...] = ()


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    arguments: dict
    invoke: Callable[[dict, InvocationContext], Outcome]
    execution: ExecutionPolicy = field(default_factory=ExecutionPolicy)


@dataclass(frozen=True)
class Contribution:
    """A replacement for this module's current contribution; None keeps it unchanged."""
    prefix: dict[str, str] = field(default_factory=dict)
    message: str = ''
    tools: tuple[Tool, ...] = ()
    exclusive: bool = False
    reset_ready: bool = True
    observations: tuple[Observation, ...] = ()
    system_instructions: str = ''


@dataclass(frozen=True)
class EventReply:
    contribution: Contribution | None = None
    controls: tuple[Control, ...] = ()


class Extension(Protocol):
    name: str

    def on_event(self, event: Event) -> EventReply | Contribution | None: ...


def validate_controls(controls, *, interrupt_allowed=False):
    if type(controls) is not tuple:
        raise AgentError('controls must be a tuple')
    for control in controls:
        if (not isinstance(control, Control) or control.kind not in ('reset', 'interrupt', 'stop', 'pause')
                or type(control.reason) is not str or not control.reason.strip()
                or type(control.finished) is not bool
                or (control.finished and control.kind != 'stop')
                or (control.kind == 'interrupt' and not interrupt_allowed)):
            raise AgentError('invalid control request at this boundary')
    if sum(c.kind in ('stop', 'pause') for c in controls) > 1:
        raise AgentError('conflicting terminal controls')


def validate_observations(observations):
    import base64
    if type(observations) is not tuple or len(observations) > 4:
        raise AgentError('observations must be a tuple of at most four items')
    total = 0
    for item in observations:
        if (not isinstance(item, Observation) or type(item.text) is not str
                or type(item.image) is not str or not (item.text or item.image)
                or item.lifetime not in ('next_request', 'until_replaced') or len(item.text) > 24000):
            raise AgentError('invalid observation')
        if item.image:
            header, sep, encoded = item.image.partition(',')
            if header not in ('data:image/png;base64', 'data:image/jpeg;base64') or not sep:
                raise AgentError('image observation must be embedded PNG or JPEG')
            if len(encoded) > 12_000_000:
                raise AgentError('image observation exceeds limit')
            try:
                data = base64.b64decode(encoded, validate=True)
            except ValueError as exc:
                raise AgentError('invalid image encoding') from exc
            if not data or (header.endswith('png;base64') and not data.startswith(b'\x89PNG\r\n\x1a\n')) or (
                    header.endswith('jpeg;base64') and not data.startswith(b'\xff\xd8\xff')):
                raise AgentError('image content does not match media type')
            total += len(data)
    if total > 16_000_000:
        raise AgentError('observation images exceed combined limit')


@dataclass(frozen=True)
class TurnView:
    """Private snapshot for one request. Its tools cannot change while a reply is pending."""
    prefix: tuple[dict, ...]
    messages: tuple[dict, ...]
    tools: tuple[Tool, ...]
    exclusive_owner: str | None
    tool_owners: dict[str, str] = field(default_factory=dict)
    system_instructions: tuple[str, ...] = ()

    @staticmethod
    def schema_for(tool):
        args = tool.arguments
        return {'type': 'object', 'description': tool.description, 'properties': {
            'action': {'const': tool.name}, **copy.deepcopy(args.get('properties', {}))},
            'required': ['action', *args.get('required', [])], 'additionalProperties': False}

    def response_format(self):
        branches = [self.schema_for(tool) for tool in self.tools]
        if not branches:
            raise AgentError('no callable functions in this turn; module constraints cannot make progress')
        schema = branches[0] if len(branches) == 1 else {'anyOf': branches}
        return {'type': 'json_schema', 'json_schema': {
            'name': 'worker_action', 'strict': True, 'schema': schema}}

    def tool_for(self, action):
        for tool in self.tools:
            if action.get('action') == tool.name:
                return tool
        raise AgentError('function is unavailable in this turn: ' + str(action.get('action')))

    def invoke(self, action, context):
        # Dispatch against precisely the descriptions used for THIS model request.
        json.dumps(action, allow_nan=False)
        for tool in self.tools:
            if action.get('action') == tool.name:
                Draft202012Validator(self.schema_for(tool)).validate(action)
                return tool.invoke(copy.deepcopy({k: v for k, v in action.items() if k != 'action'}),
                                   copy.deepcopy(context))
        raise AgentError('function is unavailable in this turn: ' + str(action.get('action')))


class EventHost:
    """Broadcast lifecycle events and compose module output; contains no domain policy."""

    def __init__(self, extensions):
        self.extensions = tuple(extensions)
        names = [module.name for module in self.extensions]
        if any(not isinstance(n, str) or not n or not n.isidentifier() for n in names):
            raise AgentError('extension names must be nonempty identifiers')
        if len(set(names)) != len(names):
            raise AgentError('duplicate extension name')
        self.contributions = {name: Contribution() for name in names}
        self.started = []
        self.controls = []
        self.pending_observations = {name: () for name in names}

    @staticmethod
    def _freeze(value):
        if not isinstance(value, Contribution):
            raise AgentError('event handler must return Contribution or None')
        if (type(value.prefix) is not dict or any(type(k) is not str or type(v) is not str
                for k, v in value.prefix.items()) or type(value.message) is not str
                or type(value.exclusive) is not bool or type(value.reset_ready) is not bool
                or type(value.system_instructions) is not str or type(value.tools) is not tuple):
            raise AgentError('invalid module contribution')
        validate_observations(value.observations)
        tools = []
        for tool in value.tools:
            if (not isinstance(tool, Tool) or not tool.name.isidentifier() or not callable(tool.invoke)
                    or type(tool.description) is not str or not tool.description.strip() or type(tool.arguments) is not dict or tool.arguments.get('type') != 'object'
                    or tool.arguments.get('additionalProperties') is not False
                    or 'action' in tool.arguments.get('properties', {})):
                raise AgentError('invalid function declaration')
            if (not isinstance(tool.execution, ExecutionPolicy)
                    or type(tool.execution.release_model) is not bool
                    or (tool.execution.restore_guard is not None and not callable(tool.execution.restore_guard))
                    or (tool.execution.restore_guard is not None and not tool.execution.release_model)):
                raise AgentError('invalid execution policy')
            Draft202012Validator.check_schema(tool.arguments)
            tools.append(Tool(tool.name, tool.description, copy.deepcopy(tool.arguments), tool.invoke, tool.execution))
        return Contribution(copy.deepcopy(value.prefix), value.message, tuple(tools),
                            value.exclusive, value.reset_ready, copy.deepcopy(value.observations), value.system_instructions)

    def emit(self, event):
        if event.kind == 'session_started':
            targets = self.extensions
        else:
            targets = self.started
        errors = []
        for module in targets:
            if event.kind == 'session_started':
                # Retain ownership even if startup fails halfway through its hook.
                self.started.append(module)
            try:
                result = module.on_event(copy.deepcopy(event))
                if event.kind == 'session_started' and (result is None or
                        (isinstance(result, EventReply) and result.contribution is None)):
                    raise AgentError('module did not provide an initial contribution')
                if isinstance(result, EventReply):
                    validate_controls(result.controls, interrupt_allowed=event.kind == 'reasoning_fragment')
                    frozen = self._freeze(result.contribution) if result.contribution is not None else None
                    self.controls.extend(result.controls)
                    result = frozen
                if result is not None:
                    self.contributions[module.name] = self._freeze(result)
            except Exception as exc:
                if event.kind == 'session_started':
                    raise AgentError(f'extension {module.name} failed during {event.kind}: {exc}') from exc
                errors.append(f'{module.name}: {type(exc).__name__}: {exc}')
        if errors:
            raise AgentError('extension event failed: ' + '; '.join(errors))

    def snapshot(self):
        all_names, owners = {}, []
        for name, contribution in self.contributions.items():
            if contribution.exclusive:
                owners.append(name)
            for tool in contribution.tools:
                if tool.name in all_names:
                    raise AgentError(f'function name conflict: {tool.name} ({all_names[tool.name]}, {name})')
                all_names[tool.name] = name
        if len(owners) > 1:
            raise AgentError('exclusive mode conflict: ' + ', '.join(owners))
        owner = owners[0] if owners else None
        prefix, messages, tools, observations, system = [], [], [], [], []
        for name, contribution in self.contributions.items():
            for key, text in contribution.prefix.items():
                if key == 'instructions':
                    if (owner is None or owner == name) and text.strip():
                        system.append(f'MODULE {name}:\n{text.strip()}')
                else:
                    prefix.append({'role': 'user', 'content': f'EXTENSION {name}/{key}:\n{text}'})
            if contribution.message:
                messages.append({'role': 'user', 'content': f'EXTENSION {name}:\n{contribution.message}'})
            active_observations = (*contribution.observations, *self.pending_observations[name])
            observations.extend(active_observations)
            for observation in active_observations:
                messages.append({'role': 'user', 'content': [
                    {'type': 'text', 'text': f'EXTENSION {name}/observation:'}, *observation.parts()]})
            if owner is None or owner == name:
                if contribution.system_instructions.strip():
                    system.append(f'MODULE {name}:\n{contribution.system_instructions.strip()}')
                for tool in contribution.tools:
                    system.append(f'ACTION {tool.name}: {tool.description.strip()}')
                tools.extend(contribution.tools)
        validate_observations(tuple(observations))
        return TurnView(tuple(prefix), tuple(messages), tuple(tools), owner, all_names, tuple(system))

    def model_history(self, history):
        """Remove selected complete turns only from the model's view."""
        records = copy.deepcopy(history)
        by_turn = {record['turn_id']: record for record in records}
        removed = set()
        for module in self.started:
            get_turns = getattr(module, 'turns_to_remove', None)
            if not callable(get_turns):
                continue
            try:
                turns = get_turns()
                if type(turns) is not tuple or any(type(turn) is not int or turn < 1 for turn in turns):
                    raise AgentError('turns to remove must be a tuple of positive integers')
                for turn in turns:
                    if turn not in by_turn:
                        continue
                    if any(str(message.get('content', '')).startswith('USER REPLY:\n')
                           for message in by_turn[turn]['messages']):
                        continue
                    removed.add(turn)
            except Exception as exc:
                raise AgentError(f'extension {module.name} failed while rendering history: {exc}') from exc
        return [record for record in records if record['turn_id'] not in removed]

    def reset_ready(self):
        return all(c.reset_ready for c in self.contributions.values())

    def consume_observations(self, view):
        from dataclasses import replace
        for name, contribution in self.contributions.items():
            self.contributions[name] = replace(contribution, observations=tuple(
                o for o in contribution.observations if o.lifetime == 'until_replaced'))
            self.pending_observations[name] = tuple(
                o for o in self.pending_observations[name] if o.lifetime == 'until_replaced')

    def take_controls(self, kind):
        taken = tuple(c for c in self.controls if c.kind == kind)
        self.controls = [c for c in self.controls if c.kind != kind]
        return taken

    def reset_requested(self):
        return any(c.kind == 'reset' for c in self.controls)

    def add_outcome(self, tool_name, outcome, view):
        validate_controls(outcome.controls)
        validate_observations(outcome.observations)
        self.controls.extend(outcome.controls)
        name = view.tool_owners[tool_name]
        self.pending_observations[name] = copy.deepcopy(outcome.observations)

    def stop(self, event):
        errors, failed = [], []
        for module in reversed(self.started):
            try:
                module.on_event(copy.deepcopy(event))
            except BaseException as exc:
                errors.append(f'{module.name}: {type(exc).__name__}: {exc}')
                failed.append(module)
        self.started = list(reversed(failed))
        self.contributions = {m.name: self.contributions[m.name] for m in self.started}
        if errors:
            raise AgentError('extension cleanup failed: ' + '; '.join(errors))
