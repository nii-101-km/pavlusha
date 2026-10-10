"""Real GUI lifetime regression: no process mocks, the session survives repeated idle periods."""
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from tools.smoke_gui_lifetime import run


@unittest.skipUnless(os.getenv('PAVLUSHA_TEST_GUI_BROWSER')=='1' and shutil.which('Xvfb') and shutil.which('bwrap'),
                     'enable real private browser tests with PAVLUSHA_TEST_GUI_BROWSER=1')
class GuiLifetimeTests(unittest.TestCase):
    def test_same_browser_server_survive_idle_shell_input_and_cleanup_cycles(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run(Path(tmp), idle=12)
            self.assertEqual(result['result'], 'PASS')
            self.assertGreaterEqual(result['elapsed_since_start_return'], 12)
            self.assertEqual(result['shell_turns'], [0,0])
            self.assertEqual(result['actual_textbox_value'], 'AbC_XyZ-123')
            self.assertEqual(result['owned_live_after_close'], [])
            self.assertEqual(result['restart_cleanup_cycles'], 2)
            self.assertTrue(result['essential_original_pids'])
