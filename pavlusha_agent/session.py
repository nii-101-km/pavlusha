"""Technical session persistence. Contains no module-owned state or handoff schema."""
import copy
import hashlib
import json
import os
from pathlib import Path
import uuid

from .core import AgentError


class Session:
    def __init__(self, directory, task):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / 'session.json'
        self.resumed = self.path.exists()
        if self.resumed:
            self.state = json.loads(self.path.read_text())
            if (self.state.get('version') != 1 or self.state.get('task') != task
                    or self.state.get('task_sha256') != hashlib.sha256(task.encode()).hexdigest()):
                raise AgentError('session task/version mismatch; use a separate session directory')
            if self.state.get('pending'):
                raise AgentError('previous action has an uncertain outcome; refusing to replay it automatically')
            if self.state.get('status') == 'finished':
                raise AgentError('session already finished; use a new session directory')
            self._discard_uncommitted_turns()
        else:
            self.state = {'version': 1, 'session_id': uuid.uuid4().hex, 'task': task,
                          'task_sha256': hashlib.sha256(task.encode()).hexdigest(),
                          'context_epoch': 0, 'turn_id': 0, 'history': [], 'pending': None,
                          'status': 'running', 'reset_reason': 'session_start'}
            self.save()

    def _discard_uncommitted_turns(self):
        path = self.directory / 'events.jsonl'
        if not path.exists():
            return
        try:
            lines = path.read_bytes().splitlines(keepends=True)
            kept = []
            changed = False
            for index, line in enumerate(lines):
                try:
                    entry = json.loads(line)
                except (ValueError, UnicodeError):
                    # An interrupted append may leave only the final record torn.
                    # Corruption in a complete record must still fail closed.
                    if index == len(lines) - 1 and not line.endswith(b'\n'):
                        changed = True
                        continue
                    raise
                number = entry.get('turn_id')
                if entry.get('kind') == 'turn_completed' and type(number) is int and number > self.state['turn_id']:
                    changed = True
                else:
                    kept.append(line if line.endswith(b'\n') else line + b'\n')
                    changed = changed or not line.endswith(b'\n')
            if changed:
                temporary = path.with_suffix('.tmp')
                with temporary.open('wb') as handle:
                    handle.writelines(kept)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
                fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        except (OSError, ValueError, TypeError) as exc:
            raise AgentError(f'cannot recover event journal: {exc}') from exc

    def save(self):
        temporary = self.path.with_suffix('.tmp')
        with temporary.open('w') as handle:
            json.dump(self.state, handle, ensure_ascii=False, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)
        fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def journal(self, kind, **data):
        with (self.directory / 'events.jsonl').open('a') as handle:
            handle.write(json.dumps({'kind': kind, **data}, ensure_ascii=False, allow_nan=False) + '\n')
            handle.flush()
            os.fsync(handle.fileno())

    def begin_action(self, action):
        self.state['pending'] = copy.deepcopy(action)
        self.save()

    def complete(self, turn, messages, *, preserve_pending=False):
        updated = copy.deepcopy(self.state)
        updated['turn_id'] = turn['turn_id']
        updated['history'].append({'turn_id': turn['turn_id'], 'messages': copy.deepcopy(messages)})
        if not preserve_pending:
            updated['pending'] = None
        if turn['stop']:
            updated['status'] = 'finished'
        self.journal('turn_completed', **turn)
        self.state = updated
        self.save()

    def completed_turns(self, after_turn=0):
        """Read committed turn events after a cursor, rejecting missing history."""
        if type(after_turn) is not int or not 0 <= after_turn <= self.state['turn_id']:
            raise AgentError('invalid event journal cursor')
        records = {}
        try:
            with (self.directory / 'events.jsonl').open() as handle:
                for line in handle:
                    entry = json.loads(line)
                    if entry.get('kind') != 'turn_completed':
                        continue
                    number = entry.get('turn_id')
                    if type(number) is not int or number < 1:
                        raise AgentError('invalid turn in event journal')
                    if after_turn < number <= self.state['turn_id']:
                        if number in records:
                            raise AgentError('duplicate committed turn in event journal')
                        records[number] = {k: v for k, v in entry.items() if k != 'kind'}
        except (OSError, ValueError, TypeError) as exc:
            raise AgentError(f'cannot read event journal: {exc}') from exc
        expected = range(after_turn + 1, self.state['turn_id'] + 1)
        if set(records) != set(expected):
            raise AgentError('event journal is missing committed turns')
        return [copy.deepcopy(records[number]) for number in expected]

    def reset(self, reason):
        self.journal('history_archived', context_epoch=self.state['context_epoch'], history=self.state['history'])
        self.state['history'] = []
        self.state['context_epoch'] += 1
        self.state['reset_reason'] = reason
        self.save()
