import unittest

from pavlusha_modules.bundled.reasoning_loop import ReasoningLoopDetector
from pavlusha_modules.loop_detection import create_extension


class ReasoningWindowConfigTests(unittest.TestCase):
    def test_custom_window_detects_repeated_long_phrase(self):
        detector = ReasoningLoopDetector(window_words=80, stride_words=20, min_lag_words=80)
        phrase = ' '.join(f'word{i}' for i in range(100)) + ' '
        signals = detector.feed(phrase * 5)
        self.assertTrue(any(signal.confirmed for signal in signals))

    def test_factory_passes_options_and_checks_invalid_values(self):
        module = create_extension(settings={'reasoning_window_words': 80,
            'reasoning_stride_words': 20, 'reasoning_min_lag_words': 80}, context={})
        self.assertEqual(module.reasoning_options['window_words'], 80)
        with self.assertRaises(ValueError):
            create_extension(settings={'reasoning_window_words': 3}, context={})


if __name__ == '__main__':
    unittest.main()
