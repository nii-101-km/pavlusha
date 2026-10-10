"""Exact cycles and controller escalation, without a live model or host sleep."""
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

import tests.test_interactive as interactive_cases
from pavlusha_agent.operation_loop import OperationLoopDetector
from tests.test_checkpoint_snapshots import done
from tests.test_reasoning_window import init_turn, turn
from tests.test_runtime_lifecycle import run_script


class OperationLoopTests(unittest.TestCase):
    def test_cycles_of_one_two_and_three_pairs(self):
        for length in (1, 2, 3):
            with self.subTest(length=length):
                detector = OperationLoopDetector()
                for index in range(3 * length):
                    letter = 'ABC'[index % length]
                    signal = detector.feed('shell', {'command': letter},
                                           {'stdout': letter, 'duration_seconds': index}, step=index+1)
                    if index < 3 * length - 1:
                        self.assertIsNone(signal)
                self.assertEqual(signal.cycle_length, length)
                self.assertEqual(signal.steps, tuple(range(1, 3*length+1)))

    def test_changed_arguments_outputs_and_nested_values_break_repetition(self):
        for field in ('command', 'stdout', 'nested_duration'):
            with self.subTest(field=field):
                detector = OperationLoopDetector()
                for index in range(9):
                    action = {'command': 'same'}
                    result = {'stdout': 'same', 'result': {'duration_seconds': 0}}
                    if field == 'command':
                        action['command'] = f'edit file version {index}'
                    elif field == 'stdout':
                        result['stdout'] = f'new result {index}'
                    else:
                        result['result']['duration_seconds'] = index
                    self.assertIsNone(detector.feed('shell', action, result, step=index))

    def test_key_order_is_irrelevant_but_list_order_is_significant(self):
        detector = OperationLoopDetector()
        for index in range(3):
            action = {'b': 2, 'a': 1} if index % 2 else {'a': 1, 'b': 2}
            signal = detector.feed('call_function', action, {'result': [1, 2]}, step=index)
        self.assertIsNotNone(signal)
        detector.clear()
        for index, value in enumerate(([1, 2], [2, 1], [1, 2])):
            self.assertIsNone(detector.feed('call_function', action, {'result': value}, step=index))

    def test_withheld_or_unexecuted_result_clears_history(self):
        for result in ({'output_withheld': True}, {'error': 'network_not_granted'},
                       {'error': 'invalid_arguments'}, {'launch_error': 'failed'}):
            with self.subTest(result=result):
                detector = OperationLoopDetector()
                for index in range(2):
                    detector.feed('shell', {'command': 'same'}, {'stdout': 'same'}, step=index)
                self.assertIsNone(detector.feed('shell', {'command': 'same'}, result, step=3))
                self.assertIsNone(detector.feed('shell', {'command': 'same'}, {'stdout': 'same'}, step=4))

    def test_bounded_memory(self):
        detector = OperationLoopDetector()
        for index in range(100):
            detector.feed('shell', {'command': str(index)}, {}, step=index)
        self.assertEqual(len(detector.history), 9)


class OperationLoopRuntimeTests(unittest.TestCase):
    def run_case(self, *args, **kwargs):
        return interactive_cases.InteractiveTests.run_case(self, *args, **kwargs)

    @unittest.skipUnless(os.environ.get('PAVLUSHA_TEST_OPERATION_LOOP_SHELL') == '1',
                         'opt-in real bubblewrap operation-loop smoke')
    def test_real_shell_three_step_cycle_pauses_after_saved_failures(self):
        from pavlusha_agent.sandbox import run_shell
        from pavlusha_agent.state_store import StateStore
        commands = [f'printf "attempt {letter}\\n"; exit 1' for letter in 'ABC'] * 3
        def paused(root, seen, executed):
            self.assertEqual(len(seen), 10)
            self.assertEqual(executed, commands)
            records = StateStore(root/'state', 'task').load()['counters']['operation']
            self.assertEqual(records, 9)
            history = str(seen[-1])
            self.assertIn('attempt C', history)
            self.assertIn('"exit_code": 1', history)
        result, seen, executed, waiting, output, _, persisted = self.run_case([
            init_turn(), *(turn({'action': 'shell', 'command': cmd}) for cmd in commands),
            done('verified'), turn({'action': 'finish', 'summary': 'Done'})],
            lines=('Stop retrying\n',), read_hook=paused, shell_runner=run_shell)
        self.assertEqual(result, 0)
        self.assertEqual(len(waiting), 1)
        self.assertEqual(len(seen), 12)
        self.assertIn('cycle of 3', output)
        self.assertIn('steps 2, 3, 4, 5, 6, 7, 8, 9, 10', output)
        self.assertIn('"kind": "operation_loop"', persisted)

    @unittest.skipUnless(os.environ.get('PAVLUSHA_TEST_OPERATION_LOOP_SHELL') == '1',
                         'opt-in real bubblewrap file-progress smoke')
    def test_real_file_progress_in_result_breaks_repetition(self):
        from pavlusha_agent.sandbox import run_shell
        command = "printf x >> progress.txt; wc -c < progress.txt"
        result, _, commands, waiting, _, _, _ = self.run_case([
            init_turn(), *(turn({'action': 'shell', 'command': command}) for _ in range(5)),
            done('verified'), turn({'action': 'finish', 'summary': 'Done'})], shell_runner=run_shell)
        self.assertEqual(result, 0)
        self.assertEqual(len(commands), 5)
        self.assertEqual(waiting, [])

    @unittest.skipUnless(os.environ.get('PAVLUSHA_TEST_OPERATION_LOOP_SHELL') == '1',
                         'opt-in real bubblewrap hidden-progress limitation')
    def test_silent_real_file_progress_still_has_identical_observed_results(self):
        from pavlusha_agent.sandbox import run_shell
        command = 'printf x >> progress.txt'
        def paused(root, *_):
            self.assertEqual((root/'work/progress.txt').read_text(), 'xxx')
        result, _, _, waiting, output, _, _ = self.run_case([
            init_turn(), *(turn({'action': 'shell', 'command': command}) for _ in range(3)),
            done('verified'), turn({'action': 'finish', 'summary': 'Done'})],
            lines=('\n',), read_hook=paused, shell_runner=run_shell)
        self.assertEqual(result, 0)
        self.assertEqual(len(waiting), 1)
        self.assertIn('OPERATION LOOP DETECTED', output)

    def test_pause_on_cycle_before_next_inference_with_results_already_recorded(self):
        for length in (1, 2, 3):
            with self.subTest(length=length):
                commands = list('ABC'[:length]) * 3
                def paused(root, seen, executed):
                    self.assertEqual(len(seen), 1+len(commands))
                    self.assertEqual(executed, commands)
                    from pavlusha_agent.state_store import StateStore
                    state = StateStore(root/'state', 'task').load()
                    self.assertEqual(state['counters']['operation'], len(commands))
                result, seen, executed, waiting, output, _, _ = self.run_case([
                    init_turn(), *(turn({'action': 'shell', 'command': cmd}) for cmd in commands),
                    done('verified'), turn({'action': 'finish', 'summary': 'Done'})],
                    lines=('Stop repeating\n',), read_hook=paused)
                self.assertEqual(result, 0)
                self.assertEqual(len(waiting), 1)
                self.assertIn('OPERATION LOOP DETECTED', output)
                self.assertIn(f'cycle of {length}', output)
                self.assertIn('Stop repeating', str(seen[-2]))

    def test_empty_resume_restarts_count_not_immediate_pause(self):
        result, _, _, waiting, _, _, _ = self.run_case([
            init_turn(), *(turn({'action': 'shell', 'command': 'A'}) for _ in range(5)),
            done('verified'), turn({'action': 'finish', 'summary': 'Done'})], lines=('\n',))
        self.assertEqual(result, 0)
        self.assertEqual(len(waiting), 1)

    def test_manual_pause_also_clears_repetition(self):
        result, _, _, waiting, output, _, _ = self.run_case([
            init_turn(), *(turn({'action': 'shell', 'command': 'A'}) for _ in range(4)),
            done('verified'), turn({'action': 'finish', 'summary': 'Done'})],
            lines=('\n',), inference_pause=4)
        self.assertEqual(result, 0)
        self.assertEqual(len(waiting), 1)
        self.assertNotIn('OPERATION LOOP DETECTED', output)

    def test_reviews_do_not_hide_repeated_operations(self):
        result, _, _, waiting, _, _, _ = self.run_case([
            init_turn(), turn({'action': 'shell', 'command': 'A'}),
            turn({'action': 'project_review_skip'}), turn({'action': 'shell', 'command': 'A'}),
            turn({'action': 'project_review_skip'}), turn({'action': 'shell', 'command': 'A'}),
            done('verified'), turn({'action': 'finish', 'summary': 'Done'})],
            lines=('\n',), extra=('--project-review-every', '1'))
        self.assertEqual(result, 0)
        self.assertEqual(waiting, [(6, ['A', 'A', 'A'])])

    def test_gui_is_excluded_and_breaks_the_operation_sequence(self):
        with patch('pavlusha_agent.gui.GuiRuntime.execute', return_value=({'state': 'closed'}, None)):
            result, _, _, waiting, _, _, _ = self.run_case([
                init_turn(), turn({'action': 'shell', 'command': 'A'}),
                turn({'action': 'shell', 'command': 'A'}), turn({'action': 'view_gui'}),
                turn({'action': 'shell', 'command': 'A'}), turn({'action': 'shell', 'command': 'A'}),
                done('verified'), turn({'action': 'finish', 'summary': 'Done'})])
        self.assertEqual(result, 0)
        self.assertEqual(waiting, [])

    def test_function_results_trigger_same_pause(self):
        with tempfile.TemporaryDirectory() as tmp:
            module = Path(tmp)/'functions.py'
            module.write_text('def probe() -> str:\n    return "same error"\n')
            call = {'action': 'call_function', 'name': 'probe', 'arguments': {}}
            result, _, _, waiting, output, _, _ = self.run_case([
                init_turn(), *(turn(call) for _ in range(3)),
                done('verified'), turn({'action': 'finish', 'summary': 'Done'})],
                lines=('\n',), extra=('--functions', str(module)))
        self.assertEqual(result, 0)
        self.assertEqual(waiting, [(4, [])])
        self.assertIn('OPERATION LOOP DETECTED', output)

    def test_batch_stops_without_extra_call_and_records_last_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seen, result, error = run_script(root, [
                init_turn(), *(turn({'action': 'shell', 'command': 'A'}) for _ in range(3))])
            self.assertIsNone(result)
            self.assertEqual(len(seen), 4)
            self.assertIn('OPERATION LOOP DETECTED', error)
            self.assertIn('--interactive', error)
            import json
            events = [json.loads(line) for line in (root/'state/experiment.jsonl').read_text().splitlines()]
            match = next(e for e in events if e['kind'] == 'operation_loop')
            self.assertEqual(match['matched_steps'], [2, 3, 4])
