"""Manual stream cancellation: transport closure, safe pause and intact execution evidence."""
import io
import json
import signal
import unittest
from unittest.mock import patch

from pavlusha_agent.core import ProviderTurn
from pavlusha_agent.provider import ChatProvider, WorkerStreamInterrupted
import tests.test_interactive as interactive_cases
from tests.test_checkpoint_snapshots import done
from tests.test_reasoning_window import init_turn, turn
from tests.test_reasoning_loop import TrackingResponse


class GenerationCancelTests(unittest.TestCase):
    def test_cancel_on_reasoning_content_tool_and_terminal_chunks_closes_stream(self):
        deltas = [{'reasoning_content': 'thinking'}, {'content': '{"action":"shell",'},
                  {'tool_calls': [{'id': 'partial', 'function': {'arguments': '{'}}]}, {}]
        for stop_at in range(1, 5):
            with self.subTest(stop_at=stop_at):
                chunks = [{'choices': [{'delta': delta, 'finish_reason': 'stop' if i == 3 else None}]}
                          for i, delta in enumerate(deltas)]
                response = TrackingResponse((''.join('data: ' + json.dumps(c) + '\n' for c in chunks)
                                             + 'data: [DONE]\n').encode())
                def cancel():
                    return response.lines_read >= stop_at
                provider = ChatProvider('http://test/v1', 'model', '', 5, 0, 100)
                with patch('urllib.request.urlopen', return_value=response), self.assertRaises(WorkerStreamInterrupted):
                    provider.worker_completion([], on_delta=lambda *_: None, should_cancel=cancel)
                self.assertTrue(response.closed)
                self.assertEqual(response.lines_read, stop_at)

    def run_case(self, *args, **kwargs):
        return interactive_cases.InteractiveTests.run_case(self, *args, **kwargs)

    def test_partial_reply_is_diagnostic_only_empty_resume_regenerates(self):
        partial = ProviderTurn(content='{"action":"shell","command":"forbidden',
                               reasoning_content='discarded private reasoning', finish_reason=None,
                               tool_calls=[{'id': 'incomplete'}])
        _, seen, commands, waiting, _, _, persisted = self.run_case([
            init_turn(), WorkerStreamInterrupted(partial),
            turn({'action': 'shell', 'command': 'allowed'}), done('verified'),
            turn({'action': 'finish', 'summary': 'Done'})], lines=('\n',), inference_interrupt=2,
            extra=('--no-live', '--reasoning-loop-recovery', 'off'))
        self.assertEqual(commands, ['allowed'])
        self.assertEqual(len(waiting), 1)
        self.assertNotIn('discarded private reasoning', str(seen[2]))
        self.assertNotIn('forbidden', str(seen[2]))
        self.assertIn('generation_interrupted', persisted)
        self.assertIn('discarded private reasoning', persisted)

    def test_completion_race_discards_finished_proposal_and_delivers_guidance(self):
        previous = signal.getsignal(signal.SIGQUIT)
        _, seen, commands, waiting, _, _, _ = self.run_case([
            init_turn(), turn({'action': 'shell', 'command': 'forbidden'}),
            turn({'action': 'shell', 'command': 'allowed'}), done('verified'),
            turn({'action': 'finish', 'summary': 'Done'})],
            lines=('New direction\n',), inference_interrupt=2)
        self.assertEqual(commands, ['allowed'])
        self.assertEqual(len(waiting), 1)
        self.assertIn('New direction', str(seen[2]))
        self.assertNotIn('forbidden', str(seen[2]))
        self.assertEqual(signal.getsignal(signal.SIGQUIT), previous)

    def test_interrupt_during_tool_waits_for_completed_operation(self):
        from pavlusha_agent.core import ShellResult
        def shell(workdir, command, **kwargs):
            signal.raise_signal(signal.SIGQUIT)
            return ShellResult(command, False, 0, False, 'completed despite interrupt', '', 0.01)
        _, seen, commands, waiting, _, _, _ = self.run_case([
            init_turn(), turn({'action': 'shell', 'command': 'running'}), done('verified'),
            turn({'action': 'finish', 'summary': 'Done'})], lines=('\n',), shell_runner=shell)
        self.assertEqual(waiting, [(2, ['running'])])
        self.assertEqual(commands, ['running'])
        self.assertIn('completed despite interrupt', str(seen[2]))

    def test_cancel_on_last_allowed_step_still_reaches_pause(self):
        partial = ProviderTurn(content='{"action":"shell"', reasoning_content='partial', finish_reason=None)
        result, _, commands, waiting, output, _, _ = self.run_case([
            init_turn(), WorkerStreamInterrupted(partial)],
            lines=('/quit\n',), inference_interrupt=2, extra=('--max-steps', '2'))
        self.assertEqual(result, 0)
        self.assertEqual(commands, [])
        self.assertEqual(len(waiting), 1)
        self.assertIn('PAUSED — safe to type', output)
