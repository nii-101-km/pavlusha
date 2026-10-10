"""User-confirmed whole-turn context removal, with factual state and effects retained."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import tests.test_interactive as interactive_cases
from pavlusha_agent.core import ShellResult
from pavlusha_agent.working_context import WorkingContext
from tests.test_checkpoint_snapshots import done
from tests.test_reasoning_window import init_turn, turn
from tests.test_runtime_lifecycle import run_script


class ContextTrimTests(unittest.TestCase):
    def test_complete_boundaries_keep_action_result_pairs_and_user_instructions(self):
        context = WorkingContext()
        for step in range(1, 5):
            context.current_step = step
            context.append({'role': 'user', 'content': f'thought {step}'}, kind='reasoning')
            context.append({'role': 'assistant', 'content': f'call {step}'})
            context.append({'role': 'user', 'content': f'result {step}'}, kind='shell_result')
        context.append({'role': 'user', 'content': 'must retain human requirement'}, kind='user_message')
        context.current_step = 5
        context.append({'role': 'assistant', 'content': 'incomplete call'})
        self.assertEqual(context.completed_turn_steps(), [1, 2, 3, 4])
        removed = context.last_turn_records(2)
        self.assertEqual(len(removed), 6)
        context.trim_last_turns(2)
        self.assertEqual(context.completed_turn_steps(), [1, 2])
        self.assertIn('must retain human requirement', str(context.messages()))
        self.assertIn('incomplete call', str(context.messages()))
        self.assertNotIn('call 3', str(context.messages()))
        self.assertNotIn('result 3', str(context.messages()))

    def test_invalid_counts_leave_context_intact(self):
        context = WorkingContext()
        context.current_step = 1
        context.append({'role': 'assistant', 'content': 'call'})
        context.append({'role': 'user', 'content': 'result'})
        before = context.step_records()
        for count in (0, -1, 2, True, '1'):
            with self.assertRaises(ValueError):
                context.trim_last_turns(count)
            self.assertEqual(context.step_records(), before)

    def run_case(self, *args, **kwargs):
        return interactive_cases.InteractiveTests.run_case(self, *args, **kwargs)

    def test_confirmed_trim_keeps_files_state_and_audit_and_discards_held_proposal(self):
        before = []
        def shell(workdir, command, **kwargs):
            (workdir/'effect.txt').write_text(command)
            return ShellResult(command, False, 0, False, 'executed ' + command, '', 0.01)
        def paused(root, *_):
            state = (root/'state/state.json').read_bytes()
            if not before:
                before.append(state)
            self.assertEqual(state, before[0])
            self.assertEqual((root/'work/effect.txt').read_text(), 'B')
        result, seen, commands, _, output, state, persisted = self.run_case([
            init_turn(), turn({'action': 'shell', 'command': 'A'}),
            turn({'action': 'shell', 'command': 'B'}, reasoning='REMOVE_THIS_REASONING'),
            done('held proposal'), done('fresh proposal'), turn({'action': 'finish', 'summary': 'Done'})],
            inference_pause=4, lines=('/trim_last_turns 1\n', 'y\n', '\n'),
            shell_runner=shell, read_hook=paused)
        self.assertEqual(result, 0)
        self.assertEqual(commands, ['A', 'B'])
        self.assertNotIn('REMOVE_THIS_REASONING', str(seen[4]))
        self.assertNotIn('SHELL RESULT (OP0002)', str(seen[4]))
        self.assertIn('SHELL RESULT (OP0001)', str(seen[4]))
        self.assertIn('CONTEXT HISTORY TRIMMED BY USER', str(seen[4]))
        self.assertIn('USE AT YOUR OWN RISK', output)
        self.assertIn('Confirm trimming? [y/N]:', output)
        self.assertIn('REMOVE_THIS_REASONING', persisted)  # Original record survives in archive.
        self.assertEqual(state['counters']['operation'], 2)
        self.assertIn('context_trim', persisted)

    def test_multi_turn_trim_preserves_current_project_state_updates(self):
        update = {'action': 'project_update', 'changes': [
            {'op': 'add_design', 'decision': 'KEEP_DURABLE_FACT', 'rationale': 'Source inspected'}]}
        result, seen, _, _, _, _, _ = self.run_case([
            init_turn(), turn({'action': 'shell', 'command': 'A'}), turn(update),
            turn({'action': 'wait_for_user', 'text': 'Ready'}), done('verified'),
            turn({'action': 'finish', 'summary': 'Done'})],
            lines=('trim_last_turns 3\n', 'Y\n', 'Continue carefully\n'))
        self.assertEqual(result, 0)
        self.assertIn('KEEP_DURABLE_FACT', str(seen[4]))
        self.assertNotIn('SHELL RESULT (OP0001)', str(seen[4]))
        self.assertIn('Continue carefully', str(seen[4]))

    def test_default_no_and_invalid_count_do_not_trim_or_reach_model_as_commands(self):
        result, seen, _, _, output, _, _ = self.run_case([
            init_turn(), turn({'action': 'shell', 'command': 'A'}),
            turn({'action': 'wait_for_user', 'text': 'Ready'}), done('verified'),
            turn({'action': 'finish', 'summary': 'Done'})],
            lines=('/trim_last_turns 999\n', '/trim_last_turns 0\n', '/trim_last_turns x\n',
                   '/trim_last_turns 1\n', '\n', '\n'))
        self.assertEqual(result, 0)
        self.assertIn('TRIM REFUSED', output)
        self.assertIn('TRIM CANCELLED', output)
        self.assertNotIn('CONTEXT HISTORY TRIMMED BY USER', str(seen[3]))
        self.assertIn('SHELL RESULT (OP0001)', str(seen[3]))
        self.assertNotIn('trim_last_turns', str(seen[3]))

    def test_cancel_then_trim_then_guidance_keeps_executed_effects(self):
        from pavlusha_agent.core import ProviderTurn
        from pavlusha_agent.provider import WorkerStreamInterrupted
        partial = ProviderTurn(content='{"action":"shell","command":"repeat',
                               reasoning_content='cancelled reasoning', finish_reason=None)
        def shell(workdir, command, **kwargs):
            with (workdir/'effects.txt').open('a') as f:
                f.write(command)
            return ShellResult(command, False, 0, False, 'same observation', '', .01)
        def paused(root, *_):
            self.assertEqual((root/'work/effects.txt').read_text(), 'AA')
        result, seen, commands, _, _, _, _ = self.run_case([
            init_turn(), turn({'action': 'shell', 'command': 'A'}),
            turn({'action': 'shell', 'command': 'A'}, reasoning='TRIM_THIS_TURN'),
            WorkerStreamInterrupted(partial), turn({'action': 'shell', 'command': 'B'}),
            done('verified'), turn({'action': 'finish', 'summary': 'Done'})],
            inference_interrupt=4, lines=('/trim_last_turns 1\n', 'y\n', 'Change strategy\n'),
            shell_runner=shell, read_hook=paused)
        self.assertEqual(result, 0)
        self.assertEqual(commands, ['A', 'A', 'B'])
        self.assertNotIn('cancelled reasoning', str(seen[4]))
        self.assertNotIn('TRIM_THIS_TURN', str(seen[4]))
        self.assertIn('Change strategy', str(seen[4]))

    def test_archive_failure_does_not_remove_active_context(self):
        from pavlusha_agent.runtime import _AgentRuntime
        from types import SimpleNamespace
        context = WorkingContext()
        context.current_step = 1
        context.append({'role': 'assistant', 'content': 'call'})
        context.append({'role': 'user', 'content': 'result'})
        runtime = SimpleNamespace(recent=context, project_state={'initialized': False}, state_dir=Path('/unused'))
        before = context.step_records()
        with patch('pavlusha_agent.runtime.project_state_message', return_value={}), \
             patch('pavlusha_agent.runtime._archive_history', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                _AgentRuntime._trim_last_turns(runtime, 1)
        self.assertEqual(context.step_records(), before)

    def test_restart_uses_normal_checkpoint_never_replays_trimmed_archive(self):
        from pavlusha_agent.runtime import _AgentRuntime
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, _, error = run_script(root, [init_turn(), turn({'action': 'shell', 'command': 'A'})],
                                    extra=('--max-steps', '2'))
            self.assertIn('maximum', error)
            context = WorkingContext()
            context.current_step = 2
            context.append({'role': 'assistant', 'content': 'FORGOTTEN_ACTION'})
            context.append({'role': 'user', 'content': 'FORGOTTEN_RESULT'})
            from pavlusha_agent.state_store import StateStore
            from pavlusha_agent.runtime import PromptBudget
            from pavlusha_agent.experiment import ExperimentRecorder
            from pavlusha_agent.operation_loop import OperationLoopDetector
            state_bytes = (root/'state/state.json').read_bytes()
            store = StateStore(root/'state', 'task')
            runtime = SimpleNamespace(recent=context, project_state=store.load()['project_state'],
                state_dir=root/'state', step=3, experiment=ExperimentRecorder(root/'state'),
                budget=PromptBudget(40000, .9), operation_loops=OperationLoopDetector())
            _AgentRuntime._trim_last_turns(runtime, 1)
            self.assertEqual((root/'state/state.json').read_bytes(), state_bytes)
            # This is the ordinary resume path, not a separate context store.
            seen, result, error = run_script(root, [done('verified'), turn({'action': 'finish', 'summary': 'Done'})])
            self.assertEqual(result, 0, error)
            self.assertNotIn('FORGOTTEN_ACTION', str(seen))
            self.assertNotIn('FORGOTTEN_RESULT', str(seen))
            self.assertEqual(store.load()['counters']['operation'], 1)
            self.assertIn('FORGOTTEN_RESULT', (root/'state/history_archive.jsonl').read_text())
