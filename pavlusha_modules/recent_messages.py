"""Carry the messages from the last three complete turns across a reset."""
import json
import os
from pathlib import Path

from pavlusha_agent.core import AgentError
from pavlusha_agent.extensions import Contribution


class RecentMessages:
    name = 'recent_messages'

    def __init__(self):
        self.path = None
        self.session_id = None
        self.epoch = 0
        self.pending = []
        self.saved = []

    def contribution(self):
        if not self.saved:
            return Contribution()
        return Contribution(prefix={'recent_messages': 'Messages from the last three turns before the reset, in order (not a summary):\n' +
                                    json.dumps(self.saved, ensure_ascii=False, indent=2)})

    def persist(self, epoch, messages):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.tmp')
        with temporary.open('w', encoding='utf-8') as f:
            json.dump({'session_id': self.session_id, 'epoch': epoch, 'messages': messages}, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, self.path)

    def on_event(self, event):
        if event.kind == 'session_started':
            self.path = Path(event.data['session_dir']) / 'modules' / self.name / 'messages.json'
            self.session_id, self.epoch = event.session_id, event.context_epoch
            if self.path.exists():
                try:
                    data = json.loads(self.path.read_text(encoding='utf-8'))
                    if data['session_id'] != self.session_id or type(data['epoch']) is not int or not isinstance(data['messages'], list):
                        raise ValueError('invalid saved messages')
                    if data['epoch'] == self.epoch:
                        self.saved = data['messages']
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    raise AgentError('cannot recover recent messages: ' + str(exc)) from exc
            return self.contribution()
        if event.kind == 'before_reset':
            self.pending = event.data['history'][-3:]
            self.persist(event.context_epoch + 1, self.pending)
        if event.kind == 'prepare_turn' and event.context_epoch != self.epoch:
            self.epoch = event.context_epoch
            self.saved = self.pending
            return self.contribution()


def create_extension(*, settings, context):
    if settings:
        raise ValueError('RecentMessages accepts no settings')
    return RecentMessages()
