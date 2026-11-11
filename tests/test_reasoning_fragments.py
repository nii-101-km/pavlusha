"""Reasoning fragments reach extension hooks during streaming, independently of UI output."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pavlusha_agent.adapters import ChatProvider
from pavlusha_agent.basic_host import BasicHost
from pavlusha_agent.core import AgentError, ShellResult
from pavlusha_agent.extensions import Contribution, Outcome
from pavlusha_agent.runtime import Runtime
from tests.test_events import Probe, Provider, reply, tool


class StreamingProvider(Provider):
    def worker_completion(self, messages, **options):
        callback = options['on_delta']
        callback('reasoning', 'Сначала ')
        callback('content', '{')
        callback('reasoning', '')
        callback('reasoning', 'проверим.\n')
        return super().worker_completion(messages, **options)


class ReasoningFragmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.effects = []

    def runtime(self, provider, modules, **options):
        def shell(*args, **kwargs):
            self.effects.append(1)
            return ShellResult('check', False, 0, False, 'OK', '', 0)
        return Runtime(provider, task='task', session_dir=self.root/'session',
            extensions=[BasicHost(self.root, shell_runner=shell), *modules], **options)

    def test_fragments_arrive_before_reply_even_without_live_output(self):
        probe = Probe()
        def while_generating(messages, options):
            self.assertEqual([e.kind for e in probe.events],
                ['session_started', 'prepare_turn', 'reasoning_fragment', 'reasoning_fragment'])
        runtime = self.runtime(StreamingProvider([reply({'action':'finish','summary':'done'})],
                                               hook=while_generating), [probe])
        self.assertEqual(runtime.run(), 'done')
        fragments = [e for e in probe.events if e.kind == 'reasoning_fragment']
        self.assertEqual([e.data for e in fragments], [
            {'text':'Сначала ', 'fragment_index':1}, {'text':'проверим.\n', 'fragment_index':2}])
        self.assertEqual([e.turn_id for e in fragments], [1, 1])
        self.assertEqual([e.context_epoch for e in fragments], [0, 0])
        self.assertEqual(len([e for e in probe.events if e.kind == 'turn_completed']), 1)
        self.assertFalse((self.root/'session/generation.jsonl').exists())
        journal = [json.loads(line) for line in (self.root/'session/events.jsonl').read_text().splitlines()]
        self.assertFalse(any(e.get('event', {}).get('kind') == 'reasoning_fragment' for e in journal))

    def test_fragment_numbers_restart_for_each_turn_and_include_new_epoch(self):
        probe = Probe()
        runtime = self.runtime(StreamingProvider([reply({'action':'shell','command':'check'}),
            reply({'action':'finish','summary':'done'})]), [probe], history_high=1)
        runtime.run()
        fragments = [e for e in probe.events if e.kind == 'reasoning_fragment']
        self.assertEqual([(e.turn_id, e.context_epoch, e.data['fragment_index']) for e in fragments],
                         [(1,0,1), (1,0,2), (2,1,1), (2,1,2)])
        self.assertEqual(self.effects, [1])

    def test_handlers_receive_separate_data_and_all_observe_fragment(self):
        def mutate(event):
            if event.kind == 'reasoning_fragment':
                event.data['text'] = 'changed'
        first, second = Probe('first', mutate), Probe('second')
        runtime = self.runtime(StreamingProvider([reply({'action':'finish','summary':'done'})]), [first, second])
        runtime.run()
        fragments = [e for e in second.events if e.kind == 'reasoning_fragment']
        self.assertEqual(fragments[0].data['text'], 'Сначала ')

    def test_handler_failure_stops_generation_and_never_executes_partial_action(self):
        def fail(event):
            if event.kind == 'reasoning_fragment':
                raise RuntimeError('observer failed')
        first, second = Probe('bad', fail), Probe('healthy')
        runtime = self.runtime(StreamingProvider([reply({'action':'shell','command':'blocked'})]), [first, second])
        with self.assertRaisesRegex(AgentError, 'observer failed'):
            runtime.run()
        self.assertEqual(self.effects, [])
        self.assertEqual(len([e for e in second.events if e.kind == 'reasoning_fragment']), 1)
        self.assertEqual(second.events[-1].kind, 'session_stopped')
        completed = next(e.data['turn'] for e in second.events if e.kind == 'turn_completed')
        self.assertIsNone(completed['action'])
        self.assertEqual(completed['status'], 'provider_error')

    def test_contribution_from_fragment_applies_to_next_turn_not_pending_reply(self):
        def hook(event):
            if event.kind == 'reasoning_fragment' and event.turn_id == 1:
                return Contribution(tools=(tool('review'),), exclusive=True)
            if event.kind == 'turn_completed' and (event.data['turn']['action'] or {}).get('action') == 'review':
                return Contribution()
        module = Probe('state', hook)
        runtime = self.runtime(StreamingProvider([reply({'action':'shell','command':'check'}),
            reply({'action':'review'}), reply({'action':'finish','summary':'done'})]), [module])
        runtime.run()
        self.assertEqual(self.effects, [1])
        first, second = runtime.provider.requests[:2]
        self.assertIn('shell', str(first['response_format']))
        self.assertIn('shell', str(second['response_format']))
        self.assertIn('RESTRICTED MODE', str(second['messages']))
        self.assertIn('review', str(second['response_format']))

    def test_actual_adapter_sse_emits_only_reasoning_fragments_once(self):
        chunks = [
            {'choices':[{'delta':{'reasoning_content':'One '}}]},
            {'choices':[{'delta':{'reasoning_content':'two'}}]},
            {'choices':[{'delta':{'content':'{"action":"finish","summary":"done"}'}, 'finish_reason':'stop'}]},
            {'choices':[], 'usage':{'prompt_tokens':20, 'completion_tokens':10}},
        ]
        data = ''.join('data: ' + json.dumps(chunk) + '\n\n' for chunk in chunks) + 'data: [DONE]\n\n'
        probe = Probe()
        provider = ChatProvider('http://local.invalid/v1', 'test', '', 10, .1, 100,
                                backend='chat-completions')
        runtime = self.runtime(provider, [probe])
        with patch('urllib.request.urlopen', return_value=io.BytesIO(data.encode())) as endpoint:
            self.assertEqual(runtime.run(), 'done')
        self.assertTrue(json.loads(endpoint.call_args.args[0].data)['stream'])
        self.assertEqual([e.data['text'] for e in probe.events if e.kind == 'reasoning_fragment'], ['One ', 'two'])
        completed = next(e.data['turn'] for e in probe.events if e.kind == 'turn_completed')
        self.assertEqual(completed['response']['reasoning_content'], 'One two')


if __name__ == '__main__':
    unittest.main()
