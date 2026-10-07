"""Active history capacity uses one hard HIGH threshold; no LOW watermark."""
import unittest

from pavlusha_agent.cli import build_parser


class HistoryCapacityTests(unittest.TestCase):
    def test_single_history_threshold_default(self):
        args = build_parser().parse_args(['task'])
        self.assertEqual(args.history_high, 200)
        self.assertFalse(hasattr(args, 'history_low'))

    def test_history_low_flag_is_removed(self):
        with self.assertRaises(SystemExit):
            build_parser().parse_args(['--history-low', '20', 'task'])


if __name__ == '__main__':
    unittest.main()
