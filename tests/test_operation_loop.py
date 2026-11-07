"""Regression coverage for heterogeneous module results and shell aliases."""
import copy
import unittest

from pavlusha_modules.bundled.operation_loop import OperationLoopDetector


class OperationLoopTests(unittest.TestCase):
    def test_json_error_shapes_do_not_crash_or_mutate_results(self):
        for error in ({'code': 'file_not_found', 'message': 'Missing file'},
                      ['missing file'], {'code': ['invalid_arguments']}):
            with self.subTest(error=error):
                detector = OperationLoopDetector()
                result = {'ok': False, 'error': error}
                original = copy.deepcopy(result)
                matches = [detector.feed('odt_inspect', {'path': 'missing.odt'},
                                        result, step=step) for step in range(1, 4)]
                self.assertIsNone(matches[0])
                self.assertEqual(matches[2].cycle_length, 1)
                self.assertEqual(result, original)

    def test_structured_admission_error_breaks_a_cycle(self):
        for error in ('invalid_arguments', {'code': 'invalid_arguments'}):
            with self.subTest(error=error):
                detector = OperationLoopDetector()
                for step in (1, 2):
                    detector.feed('tool', {}, {'ok': True}, step=step)
                self.assertIsNone(detector.feed('tool', {}, {'error': error}, step=3))
                self.assertIsNone(detector.feed('tool', {}, {'ok': True}, step=4))

    def test_shell_cycles_ignore_only_elapsed_time(self):
        for kind in ('shell', 'shell_released'):
            for cycle_length in (1, 2, 3):
                with self.subTest(kind=kind, cycle_length=cycle_length):
                    detector = OperationLoopDetector()
                    match = None
                    for step in range(3 * cycle_length):
                        index = step % cycle_length
                        result = {'exit_code': 0, 'stdout': str(index),
                                  'duration_seconds': step + .01}
                        original = result.copy()
                        match = detector.feed(kind, {'command': 'echo ' + str(index)},
                                              result, step=step + 1)
                        self.assertEqual(result, original)
                    self.assertEqual(match.cycle_length, cycle_length)

    def test_changing_output_does_not_establish_a_shell_cycle(self):
        for kind in ('shell', 'shell_released'):
            detector = OperationLoopDetector()
            for step in range(9):
                self.assertIsNone(detector.feed(kind, {'command': 'inspect'},
                    {'exit_code': 0, 'stdout': str(step), 'duration_seconds': .01}, step=step))
