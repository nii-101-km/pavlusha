"""Regression cases from dogfood: masked checks, lost output, unsafe text edits."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from pavlusha_agent import workspace_tools as tools
from pavlusha_agent.sandbox import build_bwrap_command


class WorkspaceToolsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.work = patch.object(tools, 'WORK', self.root)
        self.work.start()
        self.addCleanup(self.work.stop)

    def test_failing_check_keeps_status_and_full_large_output(self):
        report, code = tools.run_check([sys.executable, '-c',
            'import sys; print("x" * 30000); print("failure", file=sys.stderr); sys.exit(7)'])
        self.assertEqual(code, 7)
        self.assertEqual(report['status'], 'failed')
        self.assertGreater(report['output_bytes'], 30000)
        self.assertLessEqual(len(report['tail']), 4000)
        self.assertIn('failure', Path(report['log']).read_text())
        self.assertIn('x' * 30000, Path(report['log']).read_text())

    def test_error_word_is_not_a_failure_and_logs_are_unique(self):
        argv = [sys.executable, '-c', 'print("ERROR is expected in this test")']
        a, code = tools.run_check(argv)
        b, _ = tools.run_check(argv)
        self.assertEqual(code, 0)
        self.assertEqual(a['status'], 'passed')
        self.assertNotEqual(a['log'], b['log'])

    def test_timeout_retains_partial_output(self):
        report, code = tools.run_check([sys.executable, '-u', '-c',
            'import time; print("started"); time.sleep(30)'], timeout=0.2)
        self.assertEqual(code, 124)
        self.assertEqual(report['status'], 'timed_out')
        self.assertIn('started', report['tail'])
        self.assertLess(report['duration_seconds'], 5)

    def test_missing_executable_is_recorded(self):
        report, code = tools.run_check(['/nonexistent/pavlusha-check-command'])
        self.assertEqual(code, 127)
        self.assertEqual(report['status'], 'failed')
        self.assertTrue(Path(report['log']).exists())

    def test_check_rejects_scratch_symlink_outside_work(self):
        (self.root / '.scratch').symlink_to(self.root.parent, target_is_directory=True)
        with self.assertRaises(ValueError):
            tools.run_check([sys.executable, '-c', 'print("should not run")'])

    def test_replacement_is_exact_and_preserves_mode_and_newlines(self):
        target = self.root / 'app.py'
        target.write_bytes('привет\r\nold\r\n'.encode())
        target.chmod(0o750)
        (self.root / 'old').write_bytes(b'old\r\n')
        (self.root / 'new').write_bytes(b'new\r\n')
        tools.replace_text('app.py', 'old', 'new')
        self.assertEqual(target.read_bytes(), 'привет\r\nnew\r\n'.encode())
        self.assertEqual(target.stat().st_mode & 0o777, 0o750)
        with self.assertRaises(ValueError):
            tools.replace_text('app.py', 'old', 'new')
        self.assertEqual(target.read_bytes(), 'привет\r\nnew\r\n'.encode())

    def test_ambiguous_replacement_leaves_file_unchanged(self):
        target = self.root / 'app.py'
        target.write_text('old old')
        (self.root / 'old').write_text('old')
        (self.root / 'new').write_text('new')
        with self.assertRaises(ValueError):
            tools.replace_text('app.py', 'old', 'new')
        self.assertEqual(target.read_text(), 'old old')
        tools.replace_text('app.py', 'old', 'new', count=2)
        self.assertEqual(target.read_text(), 'new new')

    def test_path_escape_and_symlink_are_rejected(self):
        (self.root / 'escape').symlink_to(self.root.parent, target_is_directory=True)
        for path in ['../outside', str(self.root.parent), 'escape/outside']:
            with self.subTest(path=path), self.assertRaises(ValueError):
                tools.work_path(path)

    def test_read_and_definition_search_are_numbered(self):
        (self.root / 'app.py').write_text('import sys\n\ndef hello():\n    pass\n')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = tools.main(['read', 'app.py', '--start', '3', '--end', '4'])
        self.assertEqual(code, 0)
        self.assertEqual(output.getvalue(), '3: def hello():\n4:     pass\n')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = tools.main(['find', 'app.py', '^def '])
        self.assertEqual(code, 0)
        self.assertEqual(output.getvalue(), '3: def hello():\n')

    def test_shell_uses_pipefail_and_mounts_helper_readonly(self):
        command = build_bwrap_command(self.root, 'false | tail', network=False)
        self.assertEqual(command[-5:], ['--norc', '-o', 'pipefail', '-lc', 'false | tail'])
        destination = command.index('/opt/pavlusha/bin/pavlusha-tools')
        self.assertEqual(command[destination - 2], '--ro-bind')
        self.assertTrue(command[destination - 1].endswith('/workspace_tools.py'))

    def test_read_count_and_inclusive_end_return_the_same_200_lines(self):
        (self.root / 'app.py').write_text(''.join(f'line {i}\n' for i in range(1, 501)))
        expected = ''.join(f'{i}: line {i}\n' for i in range(200, 400))
        for options in (['--count', '200'], ['--end', '399'], []):
            with self.subTest(options=options):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = tools.main(['read', 'app.py', '--start', '200', *options])
                self.assertEqual(code, 0)
                self.assertEqual(output.getvalue(), expected)

    def test_read_count_handles_single_line_and_eof(self):
        (self.root / 'app.py').write_text('one\ntwo\nthree\n')
        for options, expected in ((['--start', '2', '--count', '1'], '2: two\n'),
                                  (['--start', '2', '--count', '200'], '2: two\n3: three\n'),
                                  (['--start', '4', '--count', '1'], '')):
            with self.subTest(options=options):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = tools.main(['read', 'app.py', *options])
                self.assertEqual(code, 0)
                self.assertEqual(output.getvalue(), expected)

    def test_read_rejects_invalid_ranges_and_suggests_exact_correction(self):
        for options in (['--start', '0'], ['--start', '-1'], ['--count', '0'],
                        ['--count', '-1'], ['--count', '201'],
                        ['--start', '200', '--end', '199'],
                        ['--start', '200', '--end', '400']):
            with self.subTest(options=options):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = tools.main(['read', 'app.py', *options])
                self.assertEqual(code, 2)
                error = json.loads(output.getvalue())['error']
                if options == ['--start', '200', '--end', '400']:
                    self.assertIn('201 lines', error)
                    self.assertIn('--start 200 --count 200', error)
                    self.assertIn('--start 200 --end 399', error)

    def test_read_count_and_end_are_mutually_exclusive(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            tools.main(['read', 'app.py', '--count', '200', '--end', '399'])
        self.assertEqual(error.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
