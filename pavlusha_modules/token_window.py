"""Carry a recent suffix of whole messages, measured by llama-server."""
import json
import os
from pathlib import Path
import urllib.error
import urllib.request

from pavlusha_agent.core import AgentError
from pavlusha_agent.extensions import Contribution


class TokenWindow:
    name = 'token_window'

    def __init__(self, endpoint, context_tokens=10000, retain_fraction=0.25, single_fraction=0.4):
        self.endpoint = endpoint.rstrip('/')
        self.retain_limit = int(context_tokens * retain_fraction)
        self.single_limit = int(context_tokens * single_fraction)
        self.path = None
        self.session_id = None
        self.epoch = 0
        self.pending = []
        self.saved = []

    def count(self, message):
        content = message.get('content')
        if not isinstance(content, str):
            raise AgentError('token_window: only text messages are supported')
        request = urllib.request.Request(self.endpoint + '/tokenize',
            data=json.dumps({'content': content, 'add_special': False}).encode(),
            headers={'Content-Type': 'application/json'}, method='POST')
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                tokens = json.load(response)['tokens']
            if not isinstance(tokens, list):
                raise ValueError('tokens must be a list')
            return len(tokens)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise AgentError('token_window: tokenizer failed: ' + str(exc)) from exc

    def choose(self, history):
        messages = [m for turn in history for m in turn.get('messages', [])]
        selected, total = [], 0
        for message in reversed(messages):
            count = self.count(message)
            if count > self.single_limit:
                return [], 0, f'message exceeds {self.single_limit} tokens ({count}); history cleared'
            if count > self.retain_limit:
                if not selected:
                    selected.append(message)
                    total += count
                break
            if selected and total + count > self.retain_limit:
                break
            selected.append(message)
            total += count
        return list(reversed(selected)), total, 'recent messages retained'

    def contribution(self):
        if not self.saved:
            return Contribution()
        return Contribution(prefix={'recent_messages': 'Whole recent messages retained from before the reset (not a summary):\n' +
                                    json.dumps(self.saved, ensure_ascii=False, indent=2)})

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
                    raise AgentError('token_window: cannot recover messages: ' + str(exc)) from exc
            return self.contribution()
        if event.kind == 'before_reset':
            self.pending, count, reason = self.choose(event.data['history'])
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix('.tmp')
            with temporary.open('w', encoding='utf-8') as f:
                json.dump({'session_id': self.session_id, 'epoch': event.context_epoch + 1,
                           'messages': self.pending, 'content_tokens': count, 'decision': reason}, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, self.path)
        if event.kind == 'prepare_turn' and event.context_epoch != self.epoch:
            self.epoch = event.context_epoch
            self.saved = self.pending
            return self.contribution()


def create_extension(*, settings, context):
    if set(settings) - {'tokenizer_url', 'context_tokens', 'retain_fraction', 'single_fraction'}:
        raise ValueError('token_window accepts tokenizer_url, context_tokens, retain_fraction and single_fraction')
    endpoint = settings.get('tokenizer_url', str(context.get('base_url', '')).removesuffix('/v1'))
    if not isinstance(endpoint, str) or not endpoint:
        raise ValueError('token_window requires a tokenizer_url or model base_url')
    capacity = settings.get('context_tokens', context.get('worker_context_budget', 10000))
    if 'context_tokens' in settings and context.get('worker_context_budget') is not None and capacity != context['worker_context_budget']:
        raise ValueError('token_window context_tokens must match worker_context_budget')
    retain = settings.get('retain_fraction', 0.25)
    single = settings.get('single_fraction', 0.4)
    if type(capacity) is not int or capacity <= 0 or type(retain) not in (int, float) or type(single) not in (int, float) or not 0 < retain <= single <= 1:
        raise ValueError('token_window requires positive context_tokens and 0 < retain_fraction <= single_fraction <= 1')
    return TokenWindow(endpoint, capacity, retain, single)
