"""Sequential delegation to another Pavlusha with a separate workspace."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from uuid import uuid4

from pavlusha_agent.core import AgentError, ProcessCleanupError
from pavlusha_agent.extensions import Contribution, Outcome
from .orchestration_kv import SlotSnapshot
from .util import declare, TEXT

PATHS = {'type': 'array', 'items': TEXT}


def _inside(root, value):
    path = (root / value).resolve()
    if not path.is_relative_to(root):
        raise AgentError('path must stay inside the workspace')
    return path


def _copy(source, target):
    if source.is_symlink():
        raise AgentError('symbolic links cannot be transferred')
    if source.is_dir():
        if target.exists():
            for item in source.rglob('*'):
                destination = target / item.relative_to(source)
                if destination.exists() and (not item.is_dir() or not destination.is_dir()):
                    raise AgentError(f'destination already exists: {destination}')
        shutil.copytree(source, target, dirs_exist_ok=True, symlinks=True)
    elif source.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise AgentError(f'destination already exists: {target}')
        shutil.copy2(source, target)
    else:
        raise AgentError(f'source does not exist: {source}')


class Orchestration:
    name = 'orchestration'

    def __init__(self, context):
        self.context = context
        self.work = Path(context['workdir']).resolve()
        self.children = None

    def on_event(self, event):
        if event.kind == 'session_started':
            self.children = Path(event.data['session_dir']).resolve() / 'children'
            return Contribution(message=(
                'Use delegate_agent for a sequential child agent. It gets a separate workspace. '
                'inputs are paths relative to /work; use "." for the whole workspace. '
                'Use collect_agent to copy selected results back without overwriting files.'), tools=(
                declare('delegate_agent', 'Run a child agent and wait for it.',
                        {'task': TEXT, 'inputs': PATHS}, ('task', 'inputs'), self.delegate),
                declare('collect_agent', 'Copy child files to the same paths in this workspace.',
                        {'child_id': TEXT, 'paths': PATHS}, ('child_id', 'paths'), self.collect),))

    def _command(self, task, work, state):
        ctx = self.context
        if not ctx.get('server_control_path') or not ctx.get('kv_storage_dir'):
            raise AgentError('delegation requires --enable-server-control and preserve_kv')
        if ctx['backend'] != 'llama-cpp':
            raise AgentError('delegation currently requires llama-cpp')
        terminal = shutil.which('gnome-terminal')
        if not terminal or not os.environ.get('DISPLAY'):
            raise AgentError('delegation requires a graphical terminal and DISPLAY')
        script = Path(__file__).resolve().parents[1] / 'project_state_agent.py'
        cmd = [terminal, '--wait', '--', sys.executable,
               str(Path(__file__).with_name('child_console.py')), str(work.parent / 'exit.json'),
               sys.executable, str(script),
               '--workdir', str(work), '--session-dir', str(state),
               '--config', ctx['modules_path'], '--server-control-path', ctx['server_control_path'],
               '--kv-storage-dir', ctx['kv_storage_dir']]
        cmd.extend(('--', task))
        return cmd

    def delegate(self, args, invocation):
        child_id = uuid4().hex
        child = self.children / child_id
        work, state = child / 'work', child / 'state'
        command = self._command(args['task'], work, state)
        work.mkdir(parents=True)
        for item in args['inputs']:
            source = _inside(self.work, item)
            target = work / source.relative_to(self.work)
            _copy(source, target)
        snapshot = SlotSnapshot(self.context['base_url'], self.context['kv_storage_dir'],
                                self.context['api_timeout'])
        snapshot.save()
        try:
            try:
                result = subprocess.run(command, check=False)
            except BaseException as exc:
                raise ProcessCleanupError('child termination unconfirmed; KV restore blocked') from exc
            marker = child / 'exit.json'
            try:
                exit_code = json.loads(marker.read_text())['exit_code']
                if type(exit_code) is not int or exit_code != 0:
                    raise ValueError('child did not exit successfully')
            except (OSError, ValueError, TypeError, KeyError) as exc:
                raise ProcessCleanupError('child cleanup unconfirmed; KV restore blocked') from exc
        except ProcessCleanupError:
            raise
        else:
            try:
                snapshot.restore()
                kv_status = 'restored'
            except AgentError:
                kv_status = 'cold_rebuild'
        status, summary = 'failed', ''
        session_file = state / 'session.json'
        if session_file.exists():
            data = json.loads(session_file.read_text())
            status = data.get('status', 'unknown')
            journal = state / 'events.jsonl'
            if journal.exists():
                for line in journal.read_text().splitlines():
                    entry = json.loads(line)
                    if entry.get('kind') == 'turn_completed' and entry.get('stop'):
                        summary = str(entry.get('result', {}).get('summary', ''))
        return Outcome({'child_id': child_id, 'status': status, 'exit_code': exit_code,
                        'summary': summary, 'kv_status': kv_status})

    def collect(self, args, invocation):
        child_id = args['child_id']
        if len(child_id) != 32 or any(c not in '0123456789abcdef' for c in child_id):
            raise AgentError('invalid child ID')
        root = (self.children / child_id / 'work').resolve()
        if not root.is_dir():
            raise AgentError('unknown child')
        for item in args['paths']:
            source = _inside(root, item)
            _copy(source, _inside(self.work, str(source.relative_to(root))))
        return Outcome({'child_id': child_id, 'copied': args['paths']})


class ArishkaDelegation:
    name = 'orchestration'

    def __init__(self, settings, context):
        self.settings, self.context = settings, context
        self.work = Path(context['workdir']).resolve()
        self.children = None

    def on_event(self, event):
        if event.kind == 'session_started':
            self.children = Path(event.data['session_dir']).resolve() / 'children'
            return Contribution(message=('Call delegate_agent to give a focused task to a local Pavlusha. '
                'Each child has a separate workspace. Use collect_agent for newly created output files.'), tools=(
                declare('delegate_agent', 'Run a local child agent and wait for its result.',
                        {'task': TEXT, 'inputs': PATHS}, ('task', 'inputs'), self.delegate),
                declare('collect_agent', 'Copy selected child files without overwriting existing files.',
                        {'child_id': TEXT, 'paths': PATHS}, ('child_id', 'paths'), self.collect)))

    def delegate(self, args, invocation):
        terminal = shutil.which('gnome-terminal')
        if not terminal or not os.environ.get('DISPLAY'):
            raise AgentError('delegation requires a graphical terminal')
        child_id = uuid4().hex
        child = self.children / child_id
        work, state = child / 'work', child / 'state'
        work.mkdir(parents=True)
        for value in args['inputs']:
            source = _inside(self.work, value)
            _copy(source, work / source.relative_to(self.work))
        root = Path(__file__).resolve().parents[1]
        manifest_path = Path(self.settings['modules']).expanduser()
        if not manifest_path.is_absolute():
            manifest_path = Path(self.context.get('config_dir', '.')) / manifest_path
        try:
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
            modules = manifest['modules']
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise AgentError('cannot load child modules: ' + str(exc)) from exc
        child_config = child / 'assembly.json'
        child_config.write_text(json.dumps({'modules': modules, 'module_base_dir': str(manifest_path.resolve().parent), 'runtime': {
            'backend': 'llama-cpp', 'base_url': self.settings['base_url'], 'model': self.settings['model'],
            'worker_context_budget': self.settings.get('context_budget', 16384),
            'max_steps': self.settings.get('max_steps', 80), 'review_every': 20,
            'interactive': True, 'live': True}}, ensure_ascii=False), encoding='utf-8')
        cmd = [terminal, '--wait', '--title=Pavlusha — local child', '--', sys.executable,
               str(root/'pavlusha_modules/child_console.py'), str(child/'exit.json'),
               sys.executable, '-u', str(root/'project_state_agent.py'),
               '--workdir', str(work), '--session-dir', str(state), '--config', str(child_config), '--', args['task']]
        env = os.environ.copy()
        env.pop('PAVLUSHA_API_KEY', None)
        result = subprocess.run(cmd, check=False, env=env)
        try:
            exit_code = json.loads((child/'exit.json').read_text())['exit_code']
        except (OSError, ValueError, KeyError, TypeError):
            raise AgentError('child termination was not confirmed') from None
        if type(exit_code) is not int or result.returncode != 0 or exit_code != 0:
            raise AgentError(f'child failed with exit code {exit_code}')
        session = json.loads((state/'session.json').read_text())
        summary = ''
        for line in (state/'events.jsonl').read_text().splitlines():
            entry = json.loads(line)
            if entry.get('kind') == 'turn_completed' and entry.get('stop'):
                summary = str(entry.get('result', {}).get('summary', ''))
        return Outcome({'child_id': child_id, 'status': session.get('status'), 'summary': summary})

    def collect(self, args, invocation):
        child_id = args['child_id']
        if len(child_id) != 32 or any(c not in '0123456789abcdef' for c in child_id):
            raise AgentError('invalid child ID')
        root = (self.children / child_id / 'work').resolve()
        if not root.is_dir():
            raise AgentError('unknown child')
        for item in args['paths']:
            source = _inside(root, item)
            _copy(source, _inside(self.work, str(source.relative_to(root))))
        return Outcome({'child_id': child_id, 'copied': args['paths']})


def create_extension(*, settings, context):
    if not settings:
        return Orchestration(context)
    if set(settings) - {'modules', 'base_url', 'model', 'context_budget', 'max_steps'} or not all(
            isinstance(settings.get(k), str) and settings[k] for k in ('modules', 'base_url', 'model')):
        raise ValueError('local child requires modules, base_url and model')
    return ArishkaDelegation(settings, context)
