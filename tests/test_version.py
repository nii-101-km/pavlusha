"""The two entry points and live display share the release version."""
import io
from pathlib import Path
import subprocess
import sys
import unittest

from pavlusha_agent import __version__
from pavlusha_agent.live import LiveConsoleRenderer


class VersionTests(unittest.TestCase):
    def test_entry_points_report_release_without_other_arguments(self):
        root = Path(__file__).resolve().parents[1]
        self.assertEqual(__version__, '0.9.1')
        for script in ('agent.py', 'project_state_agent.py'):
            with self.subTest(script=script):
                result = subprocess.run([sys.executable, str(root / script), '--version'],
                                        cwd=root, capture_output=True, text=True, check=True)
                self.assertEqual(result.stdout.strip(), f'Pavlusha {__version__}')

    def test_live_banner_displays_release(self):
        for plain in (False, True):
            with self.subTest(plain=plain):
                stream = io.StringIO()
                renderer = LiveConsoleRenderer(stream=stream, color=False)
                if plain:
                    renderer._console = None
                renderer.start(model='example', task='example', max_steps=1)
                self.assertIn(__version__, stream.getvalue())


if __name__ == '__main__':
    unittest.main()
