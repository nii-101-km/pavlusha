"""Opt-in diagnostics and separately granted evaluation for one browser target."""
from __future__ import annotations

from collections import deque
import importlib.util
import json
from pathlib import Path
import sys
import threading
import uuid
from typing import Protocol

from pavlusha_runtime.errors import AgentError

KINDS = frozenset({'console', 'exception', 'request_failed', 'http_error'})


class BrowserDiagnosticsAdapter(Protocol):
    """A trusted adapter owns connection, target selection, buffering and cleanup."""
    def describe(self) -> dict: ...
    def read(self, *, since: int = 0, limit: int = 50, kinds: list | None = None) -> dict: ...
    def clear(self) -> dict: ...
    def close(self) -> None: ...


class BrowserSession:
    """Evaluation requires both a startup grant and an adapter capability."""
    def __init__(self, adapter, allow_evaluate=False):
        self.adapter = adapter
        self._closed = False
        self._module_name = None
        self.allow_evaluate = allow_evaluate
        if allow_evaluate and not evaluation_available(adapter):
            raise ValueError('adapter does not support requested evaluate capability')

    def describe(self):
        description = dict(self.adapter.describe())
        if not self.allow_evaluate:
            description['capabilities'] = [c for c in description.get('capabilities', []) if c != 'evaluate']
        return description

    def read(self, **kwargs):
        result = self.adapter.read(**kwargs)
        if isinstance(result, dict):
            result = {**result, 'capabilities': self.describe().get('capabilities', [])}
        return result

    def clear(self):
        result = self.adapter.clear()
        if isinstance(result, dict):
            result = {**result, 'capabilities': self.describe().get('capabilities', [])}
        return result

    def evaluate(self, **kwargs):
        if not self.allow_evaluate:
            return {'error': 'browser_evaluate_disabled', 'execution_may_have_occurred': False}
        return self.adapter.evaluate(**kwargs)

    def close(self):
        if self._closed:
            return
        self.adapter.close()
        self._closed = True
        self.adapter = None
        if self._module_name:
            sys.modules.pop(self._module_name, None)
            self._module_name = None


def evaluation_available(adapter):
    return (adapter is not None and callable(getattr(adapter, 'evaluate', None))
            and 'evaluate' in adapter.describe().get('capabilities', []))


def validate_browser_evaluate(action):
    if set(action) - {'action', 'expression', 'timeout_ms', 'await_promise'}:
        raise AgentError('browser_evaluate has unsupported fields')
    expression = action.get('expression')
    timeout, await_promise = action.get('timeout_ms', 1000), action.get('await_promise', False)
    if not isinstance(expression, str) or not expression.strip() or len(expression) > 16000:
        raise AgentError('browser_evaluate expression must be nonempty and at most 16000 characters')
    if type(timeout) is not int or not 1 <= timeout <= 10000 or type(await_promise) is not bool:
        raise AgentError('browser_evaluate requires timeout_ms in 1..10000 and boolean await_promise')
    return 'browser_evaluate', {'expression': expression, 'timeout_ms': timeout, 'await_promise': await_promise}


def evaluate_browser(adapter, data, output_limit):
    if not evaluation_available(adapter):
        return {'error': 'browser_evaluate_disabled', 'execution_may_have_occurred': False}
    try:
        payload = adapter.evaluate(**data)
        if not isinstance(payload, dict):
            raise ValueError('adapter evaluate result must be a JSON object')
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        if len(encoded) > output_limit:
            return {'error': 'output_limit_exceeded', 'execution_may_have_occurred': True,
                    'detail': 'Evaluation completed but result exceeded output limit. Do not repeat side effects.'}
        return json.loads(encoded)
    except Exception as exc:
        return {'error': 'browser_evaluation_failed', 'detail': str(exc)[:600],
                'execution_may_have_occurred': True}


class EventBuffer:
    def __init__(self, capacity=500):
        self.events = deque(maxlen=capacity)
        self.serial = 0
        self.dropped = 0
        self.lock = threading.Lock()

    def add(self, kind, **fields):
        if kind not in KINDS:
            return
        with self.lock:
            self.serial += 1
            if len(self.events) == self.events.maxlen:
                self.dropped += 1
            self.events.append({'seq': self.serial, 'kind': kind, **fields})

    def read(self, *, since=0, limit=50, kinds=None):
        with self.lock:
            found = [e.copy() for e in self.events if e['seq'] > since and (not kinds or e['kind'] in kinds)]
            selected = found[:limit]
            return {'events': selected, 'cursor': selected[-1]['seq'] if selected else since,
                    'latest': self.serial, 'has_more': len(found) > limit, 'dropped': self.dropped}

    def clear(self):
        with self.lock:
            count = len(self.events)
            self.events.clear()
            self.dropped = 0
            return {'cleared': count, 'cursor': self.serial}


def validate_browser_diagnostics(action):
    if set(action) - {'action', 'operation', 'since', 'limit', 'kinds'}:
        raise AgentError('browser_diagnostics has unsupported fields')
    operation = action.get('operation', 'read')
    if operation not in ('read', 'clear'):
        raise AgentError('browser_diagnostics operation must be read or clear')
    since, limit, kinds = action.get('since', 0), action.get('limit', 50), action.get('kinds')
    if type(since) is not int or since < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise AgentError('browser diagnostics requires since >= 0 and limit in 1..100 (integers)')
    if kinds is not None and (not isinstance(kinds, list) or not kinds
            or any(not isinstance(k, str) or k not in KINDS for k in kinds) or len(set(kinds)) != len(kinds)):
        raise AgentError('invalid browser diagnostic kinds')
    if operation == 'clear' and set(action) - {'action', 'operation'}:
        raise AgentError('clear accepts only action and operation')
    return 'browser_diagnostics', {'operation': operation, 'since': since, 'limit': limit, 'kinds': kinds}


def load_browser_adapter(path):
    """Config and module paths are controller-owned startup inputs, never Worker data."""
    config_path = Path(path).expanduser().resolve()
    adapter = None
    module_name = None
    try:
        config = json.loads(config_path.read_text())
        if not isinstance(config, dict):
            raise ValueError('config must be an object')
        allow_evaluate = config.get('allow_evaluate', False)
        if type(allow_evaluate) is not bool:
            raise ValueError('allow_evaluate must be boolean')
        kind = config.get('adapter')
        if kind == 'cdp':
            from ._cdp import CdpDiagnosticsAdapter
            adapter = CdpDiagnosticsAdapter(config)
        elif kind == 'module':
            if set(config) - {'adapter', 'path', 'settings', 'allow_evaluate'}:
                raise ValueError('unknown module adapter configuration fields')
            module_path = Path(config['path']).expanduser()
            if not module_path.is_absolute():
                module_path = config_path.parent / module_path
            module_name = '_pavlusha_browser_adapter_' + uuid.uuid4().hex
            spec = importlib.util.spec_from_file_location(module_name, module_path.resolve())
            if spec is None or spec.loader is None:
                raise ValueError('cannot load adapter module')
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            try:
                spec.loader.exec_module(module)
            except BaseException:
                sys.modules.pop(module_name, None)
                raise
            adapter = module.create_adapter(config.get('settings', {}))
        else:
            raise ValueError('adapter must be cdp or module')
        for method in ('describe', 'read', 'clear', 'close'):
            if not callable(getattr(adapter, method, None)):
                raise ValueError('adapter missing method: ' + method)
        description = adapter.describe()
        # Bound metadata too; custom modules are trusted code, not an isolation boundary.
        encoded = json.dumps(description, ensure_ascii=False)
        if not isinstance(description, dict) or len(encoded) > 8000:
            raise ValueError('describe() must return a bounded JSON object')
        session = BrowserSession(adapter, allow_evaluate)
        session._module_name = module_name
        return session
    except BaseException as exc:
        cleanup_error = None
        if adapter is not None and callable(getattr(adapter, 'close', None)):
            try:
                adapter.close()
            except BaseException as cleanup:
                cleanup_error = cleanup
        if module_name:
            sys.modules.pop(module_name, None)
        if cleanup_error:
            raise AgentError(f'browser initialization failed: {exc}; cleanup failed: {cleanup_error}') from exc
        raise AgentError(f'cannot initialize browser diagnostics: {exc}') from exc


def read_browser_diagnostics(adapter, data, output_limit):
    """Recoverable feedback and a bounded output, even for user-provided adapters."""
    try:
        if adapter is None:
            raise ValueError('browser diagnostics not connected')
        payload = (adapter.clear() if data['operation'] == 'clear' else
                   adapter.read(since=data['since'], limit=data['limit'], kinds=data['kinds']))
        if not isinstance(payload, dict):
            raise ValueError('browser adapter result must be an object')
        # Copy so admission cannot mutate the adapter's internal ring.
        payload = json.loads(json.dumps(payload, ensure_ascii=False, allow_nan=False))
        if len(json.dumps(payload, ensure_ascii=False)) <= output_limit:
            return payload
        # Retain earliest events and a truthful cursor; omitted events can be read next.
        events = payload.get('events')
        if isinstance(events, list):
            payload['has_more'] = True
            while events:
                events.pop()
                payload['cursor'] = events[-1]['seq'] if events else data.get('since', 0)
                if len(json.dumps(payload, ensure_ascii=False)) <= output_limit:
                    if not events:
                        return {'error': 'output_limit_exceeded', 'hint': 'first event exceeds output limit; clear or increase --output-limit',
                                'cursor': data.get('since', 0)}
                    return payload
        return {'error': 'output_limit_exceeded', 'hint': 'browser metadata exceeds output limit'}
    except Exception as exc:
        return {'error': 'browser_diagnostics_unavailable', 'detail': (type(exc).__name__ + ': ' + str(exc))[:600]}
