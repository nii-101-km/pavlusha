"""Keep the latest observation and factual notice after confirmed cycles."""
import json
from pathlib import Path
import tempfile
import unittest

from pavlusha_agent.core import ShellResult
from pavlusha_agent.extensions import Contribution, Event, EventHost
from pavlusha_agent.runtime import Runtime
from pavlusha_modules.finish import Finish
from pavlusha_modules.loop_detection import LoopDetection
from pavlusha_modules.shell import Shell
from tests.test_events import Provider, reply


class LoopResultCompactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.work = self.root/'work'; self.work.mkdir()

    def runtime(self, replies, **options):
        shell = Shell(self.work)
        shell.host.shell_runner = lambda *a, **kw: ShellResult('check', False, 1, False, '', 'SAME FAILURE', 0.01)
        return Runtime(Provider(replies), task='fix', session_dir=self.root/'session',
                       extensions=[shell, Finish(), LoopDetection()], **options)

    def test_detected_loop_keeps_latest_turn_and_factual_notice(self):
        action = {'action':'shell','command':'check'}
        runtime = self.runtime([reply(action, reasoning='FIRST UNIQUE'), reply(action, reasoning='SECOND UNIQUE'),
                                reply(action, reasoning='THIRD UNIQUE'), reply({'action':'finish','summary':'done'})])
        self.assertEqual(runtime.run(), 'done')
        history = runtime.provider.requests[3]['messages']
        text = '\n'.join(str(m['content']) for m in history)
        self.assertGreaterEqual(text.count('SAME FAILURE'), 1)
        self.assertNotIn('FIRST UNIQUE', text)
        self.assertNotIn('SECOND UNIQUE', text)
        self.assertIn('THIRD UNIQUE', text)
        self.assertIn('Exit code: 1', text)
        self.assertIn('file state has not been checked', text)
        self.assertNotIn('Repeated action and result; no new output.', text)
        self.assertNotIn('See the full result', text)
        durable = json.loads((self.root/'session/session.json').read_text())['history']
        self.assertEqual(sum('SAME FAILURE' in str(x) for x in durable), 3)
        journal = [json.loads(x) for x in (self.root/'session/events.jsonl').read_text().splitlines()]
        self.assertEqual(sum('SAME FAILURE' in str(x.get('result')) for x in journal if x.get('kind') == 'turn_completed'), 3)

    def test_later_repeat_removes_the_new_cycle_pair(self):
        action = {'action':'shell','command':'check'}
        runtime = self.runtime([reply(action), reply(action), reply(action), reply(action),
                                reply({'action':'finish','summary':'done'})])
        self.assertEqual(runtime.run(), 'done')
        messages = runtime.provider.requests[4]['messages']
        results = [m for m in messages if str(m.get('content', '')).startswith('RESULT:\n')]
        self.assertEqual(sum('SAME FAILURE' in m['content'] for m in results), 1)
        self.assertEqual(sum('SAME FAILURE' in str(x) for x in runtime.session.state['history']), 4)

    def test_pair_with_user_reply_is_not_hidden(self):
        class RequestRemoval:
            name = 'request_removal'
            def on_event(self, event):
                return Contribution() if event.kind == 'session_started' else None
            def turns_to_remove(self):
                return (1,)
        host = EventHost([RequestRemoval()])
        host.emit(Event('session_started', 'session', 0, 0))
        history = [
            {'turn_id': 1, 'messages': [{'role':'user','content':'USER REPLY:\nKeep this instruction'}]},
            {'turn_id': 2, 'messages': [{'role':'user','content':'RESULT:\nresult'}]},
        ]
        self.assertEqual(host.model_history(history), history)

    def test_after_reset_first_result_is_shown_in_full(self):
        action = {'action':'shell','command':'check'}
        runtime = self.runtime([reply(action), reply(action), reply(action), reply(action),
                                reply({'action':'finish','summary':'done'})], history_high=3)
        self.assertEqual(runtime.run(), 'done')
        after_reset = str(runtime.provider.requests[4]['messages'])
        self.assertIn('SAME FAILURE', after_reset)
        self.assertNotIn('See the full result', after_reset)


if __name__ == '__main__': unittest.main()
