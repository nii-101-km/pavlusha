"""Active history capacity uses one hard HIGH threshold; no LOW watermark."""
import unittest
import io
from contextlib import redirect_stderr

from pavlusha_agent.cli import build_parser


class HistoryCapacityTests(unittest.TestCase):
    def test_single_history_threshold_default(self):
        args = build_parser().parse_args(['task'])
        self.assertEqual(args.history_high, 200)
        self.assertFalse(hasattr(args, 'history_low'))

    def test_history_low_flag_is_removed(self):
        with self.assertRaises(SystemExit):
            build_parser().parse_args(['--history-low', '20', 'task'])

    def test_retired_options_and_aliases_are_not_silently_accepted(self):
        for flags in (
            ['--history-window', 'off'], ['--raw-reasoning-limit', '20000'],
            ['--worker-context-control', 'drop'], ['--state-cycle-after', '2'],
            ['--context-maintenance-gate', 'on'], ['--context-meter', 'hidden'],
            ['--compact-after', '10'], ['--compact-keep', '4'], ['--no-compaction'],
            ['--context-budget', '40000'], ['--worker-context-b', '40000'],
        ):
            with self.subTest(flags=flags), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                build_parser().parse_args([*flags, 'task'])


if __name__ == '__main__':
    unittest.main()
