"""Physical input validation, failure cleanup, real X11 events and dispatch."""
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from jsonschema import ValidationError

from pavlusha_agent.gui import GuiError, PrivateDisplay, validate_gui_action, MAX_HOLD_SECONDS
from pavlusha_agent.gui_helper import keyboard
from tests.test_worker_structured_output import validator


class KeyboardContractTests(unittest.TestCase):
    def test_valid_shapes_and_modifier_order(self):
        action = {'action': 'press_key', 'key': 'l', 'modifiers': ['shift', 'ctrl']}
        kind, data = validate_gui_action(action)
        self.assertEqual(kind, 'press_key')
        self.assertEqual(data['modifiers'], ['ctrl', 'shift'])
        for action in (action, {'action': 'press_key', 'key': 'page_down'},
                       {'action': 'hold_key', 'key': 'w', 'duration': MAX_HOLD_SECONDS}):
            validator(initialized=True, gui_enabled=True).validate(action)
            validate_gui_action(action)

    def test_invalid_shapes_fail_closed_in_core_and_schema(self):
        actions = [
            {'action': 'press_key', 'key': key} for key in ('W', 'ц', 'Ctrl+L', '', 'bogus', None, [])
        ] + [
            {'action': 'press_key', 'key': 'l', 'modifiers': mods}
            for mods in ('ctrl', None, ['ctrl', 'ctrl'], ['bogus'], [[]])
        ] + [
            {'action': 'hold_key', 'key': 'w', 'duration': value}
            for value in (True, 0, -1, 2.001, '1', None)
        ] + [
            {'action': 'hold_key', 'key': 'w'},
            {'action': 'hold_key', 'key': 'w', 'duration': 1, 'modifiers': ['ctrl']},
            {'action': 'press_key', 'key': 'w', 'duration': 1},
        ]
        for action in actions:
            with self.subTest(action=action):
                with self.assertRaises(GuiError):
                    validate_gui_action(action)
                with self.assertRaises(ValidationError):
                    validator(initialized=True, gui_enabled=True).validate(action)
        for value in (math.nan, math.inf, -math.inf):
            with self.assertRaises(GuiError):
                validate_gui_action({'action': 'hold_key', 'key': 'w', 'duration': value})

    def test_keyboard_hidden_by_existing_gui_and_phase_gates(self):
        for flags in ({'gui_enabled': False}, {'gui_enabled': True, 'checkpoint_required': True},
                      {'gui_enabled': True, 'periodic_review': True}):
            for action in ({'action': 'press_key', 'key': 'w'},
                           {'action': 'hold_key', 'key': 'w', 'duration': .1}):
                with self.assertRaises(ValidationError):
                    validator(initialized=True, **flags).validate(action)


class KeyboardReleaseTests(unittest.TestCase):
    def exercise(self, failure=None):
        events = []
        X = SimpleNamespace(KeyPress=2, KeyRelease=3)
        d = SimpleNamespace(sync=lambda: None, get_display_name=lambda: ':private')
        def fake_input(_display, kind, code):
            events.append((kind, code))
            if failure == 'press' and kind == X.KeyPress and code == 25:
                raise OSError('press may already have reached X')
            if failure == 'release' and kind == X.KeyRelease and code == 25:
                raise OSError('release failed')
        def sleep(duration):
            if failure == 'interrupt':
                raise KeyboardInterrupt()
            if failure == 'timeout':
                raise TimeoutError('deadline')
            if failure == 'signal':
                os.kill(os.getpid(), signal.SIGTERM)
        try:
            with patch('pavlusha_agent.gui_helper.physical_keycodes', return_value=[37, 25]), \
                 patch('pavlusha_agent.gui_helper.time.sleep', side_effect=sleep):
                keyboard(d, X, SimpleNamespace(fake_input=fake_input),
                         {'action': 'press_key', 'key': 'w', 'modifiers': ['ctrl']}, time.monotonic()+2)
        finally:
            self.assertEqual(events, [(2, 37), (2, 25), (3, 25), (3, 37)])

    def test_press_and_reverse_release(self):
        self.exercise()

    def test_errors_deadline_and_signals_release_all_keys(self):
        for failure, error in [('press', OSError), ('release', RuntimeError),
                               ('interrupt', KeyboardInterrupt), ('timeout', TimeoutError), ('signal', TimeoutError)]:
            with self.subTest(failure=failure), self.assertRaises(error):
                self.exercise(failure)

    def test_core_timeout_and_nonzero_helper_retry_release_without_press(self):
        for failure in (subprocess.TimeoutExpired('helper', 3),
                        SimpleNamespace(returncode=1, stderr=b'failed', stdout=b'')):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                private = PrivateDisplay(Path(tmp))
                with patch('pavlusha_agent.gui.subprocess.run', side_effect=[failure, SimpleNamespace(returncode=0, stdout=b'')]) as run:
                    with self.assertRaises(GuiError):
                        private.action({'action': 'hold_key', 'key': 'w', 'duration': .2}, 3)
                    import json
                    cleanup = json.loads(run.call_args_list[1].kwargs['input'])
                    self.assertEqual(cleanup, {'action': 'release_keys', 'key': 'w', 'modifiers': []})

    def test_controller_interruption_releases_before_propagating(self):
        with tempfile.TemporaryDirectory() as tmp:
            private = PrivateDisplay(Path(tmp))
            with patch('pavlusha_agent.gui.subprocess.run', side_effect=[KeyboardInterrupt(), SimpleNamespace(returncode=0, stdout=b'')]) as run:
                with self.assertRaises(KeyboardInterrupt):
                    private.action({'action': 'press_key', 'key': 'w'}, 3)
                self.assertEqual(run.call_count, 2)

    def test_dispatch_preserves_fresh_screenshot_path(self):
        from tests.test_gui_tools import FakeDisplay, FakeProcess
        from pavlusha_agent.gui import GuiRuntime
        with tempfile.TemporaryDirectory() as tmp:
            runtime = GuiRuntime(Path(tmp), Path(tmp), network_allowed=False)
            runtime.process, runtime.display = FakeProcess(), FakeDisplay()
            for action in ({'action': 'press_key', 'key': 'l', 'modifiers': ['ctrl'], 'delay': 0},
                           {'action': 'hold_key', 'key': 'w', 'duration': .2, 'delay': 0}):
                kind, data = validate_gui_action(action)
                result, observation = runtime.execute(kind, data)
                self.assertEqual(result['key'], action['key'])
                self.assertIsNone(observation.gesture)
                self.assertEqual(runtime.display.calls[-2]['action'], kind)
                self.assertEqual(runtime.display.calls[-1]['action'], 'screenshot')
                self.assertEqual(observation.message()['content'][1]['type'], 'image_url')
            runtime.process = runtime.display = None

    def test_failed_release_retires_private_display(self):
        with tempfile.TemporaryDirectory() as tmp:
            private = PrivateDisplay(Path(tmp))
            with patch('pavlusha_agent.gui.subprocess.run', side_effect=OSError('X unavailable')), \
                 patch.object(private, '__exit__') as close:
                with self.assertRaisesRegex(GuiError, 'private display closed'):
                    private.action({'action': 'press_key', 'key': 'w'}, 3)
                close.assert_called_once()


@unittest.skipUnless(shutil.which('Xvfb') and shutil.which('setxkbmap'), 'requires private X11 tools')
class RealKeyboardTests(unittest.TestCase):
    def test_real_events_hold_duration_layout_and_text_semantics(self):
        from Xlib import X, display
        with tempfile.TemporaryDirectory() as tmp, PrivateDisplay(Path(tmp)) as private, \
             patch.dict(os.environ, private.environment):
            d = display.Display(private.environment['DISPLAY'])
            try:
                window = d.screen().root.create_window(0, 0, 300, 200, 0, X.CopyFromParent,
                    event_mask=X.KeyPressMask | X.KeyReleaseMask)
                window.map(); window.set_input_focus(X.RevertToParent, X.CurrentTime); d.sync()
                subprocess.run(['setxkbmap', '-layout', 'ru'], env=private.environment, check=True)
                before = d.get_keyboard_mapping(25, 1)
                def events():
                    d.sync()
                    result = []
                    while d.pending_events():
                        event = d.next_event()
                        if event.type in (X.KeyPress, X.KeyRelease):
                            result.append(event)
                    return result
                private.action({'action': 'press_key', 'key': 'w', 'modifiers': []}, 3)
                observed = events()
                self.assertEqual([(e.type, e.detail) for e in observed], [(X.KeyPress, 25), (X.KeyRelease, 25)])
                self.assertEqual(d.get_keyboard_mapping(25, 1), before)
                private.action({'action': 'press_key', 'key': 'l', 'modifiers': ['ctrl']}, 3)
                self.assertEqual([(e.type, e.detail) for e in events()], [(2,37),(2,46),(3,46),(3,37)])
                private.action({'action': 'hold_key', 'key': 'w', 'duration': .2}, 3)
                observed = events()
                self.assertGreaterEqual(observed[-1].time-observed[0].time, 180)
                self.assertLess(observed[-1].time-observed[0].time, 1000)
                self.assertFalse(d.query_keymap()[25//8] & (1 << (25%8)))
                with self.assertRaises(GuiError):
                    private.action({'action': 'hold_key', 'key': 'w', 'duration': 2}, .3)
                events()
                self.assertFalse(d.query_keymap()[25//8] & (1 << (25%8)))
                private.action({'action': 'type_text', 'text': 'Wц'}, 3)
                observed = events()
                self.assertEqual([e.type for e in observed], [2,3,2,3])
                self.assertTrue(all(e.detail == d.display.info.max_keycode for e in observed))
                self.assertEqual(d.get_keyboard_mapping(25, 1), before)
            finally:
                d.close()
