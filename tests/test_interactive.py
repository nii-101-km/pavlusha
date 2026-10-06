"""Deterministic safe-boundary integration with real terminal attributes, scripted Worker."""
import copy
import io
import json
import os
import pty
import signal
import select
import subprocess
import sys
import tempfile
import types
import termios
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from pavlusha_agent.cli import build_parser
from pavlusha_agent.core import AgentError, ShellResult
from pavlusha_agent.interactive import InteractiveSession, SessionEnded, validate_chat_action
from pavlusha_agent.live import LiveConsoleRenderer
from pavlusha_agent.provider import ProviderContextOverflow
from pavlusha_agent.sandbox import validate_action
from pavlusha_agent.gui import PrivateDisplay
from pavlusha_agent.runtime import run_agent
from pavlusha_agent.state_store import StateStore
from pavlusha_agent.working_context import WorkingContext
from pavlusha_agent.worker_contract import worker_response_format
from tests.test_checkpoint_snapshots import done
from tests.test_reasoning_window import init_turn, turn


class InteractiveTests(unittest.TestCase):
    def run_case(self, replies, lines=(), *, inference_pause=None,
                 shell_pause=False, extra=(), read_hook=None):
        seen, commands, waiting, sessions = [], [], [], []
        scripted = iter(replies)
        inputs = iter(lines)
        output = io.StringIO()
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        original = termios.tcgetattr(slave)
        old_handler = signal.getsignal(signal.SIGTSTP)
        with tempfile.TemporaryDirectory() as tmp, os.fdopen(slave, 'r') as tty:
            root = Path(tmp)
            def request():
                # Exercise the installed signal handler without suspending the test runner.
                with patch('pavlusha_agent.interactive.os.write'):
                    signal.raise_signal(signal.SIGTSTP)
                self.assertTrue(sessions[-1].requested)
            def worker(provider, messages, **kwargs):
                seen.append(copy.deepcopy(messages))
                if inference_pause == len(seen):
                    request()
                item = next(scripted)
                if isinstance(item, Exception):
                    raise item
                return item
            def shell(workdir, command, **kwargs):
                commands.append(command)
                if shell_pause:
                    request()
                    self.assertFalse(sessions[-1].paused)
                    self.assertFalse(waiting)  # Still executing; input cannot begin here.
                return ShellResult(command, False, 0, False, 'completed evidence', '', 0.01)
            def read(session):
                sessions.append(session)
                self.assertTrue(session.paused)
                waiting.append((len(seen), list(commands)))
                self.assertIn('PAUSED — safe to type', output.getvalue())
                if read_hook:
                    read_hook(root, seen, commands)
                return next(inputs)
            # Capture the real session before any generation (needed for signal injection).
            enter = InteractiveSession.__enter__
            def entered(session):
                result = enter(session)
                sessions.append(session)
                return result
            args = build_parser().parse_args([
                '--interactive', '--workdir', str(root/'work'), '--state-dir', str(root/'state'),
                '--model', 'scripted', '--worker-context-budget', '40000',
                '--project-review-every', '0', '--max-steps', '50', *extra, 'task'])
            with patch('pavlusha_agent.runtime.sys.stdin', tty), \
                 patch('pavlusha_agent.runtime.shutil.which', return_value='/fake/bwrap'), \
                 patch('pavlusha_agent.provider.ChatProvider.resolve_model', return_value='scripted'), \
                 patch('pavlusha_agent.provider.ChatProvider.worker_completion', worker), \
                 patch('pavlusha_agent.runtime.run_shell', shell), \
                 patch.object(InteractiveSession, '__enter__', entered), \
                 patch.object(InteractiveSession, '_readline', read), \
                 redirect_stderr(output), redirect_stdout(output):
                result = run_agent(args)
            self.assertEqual(termios.tcgetattr(tty.fileno()), original)
            self.assertEqual(signal.getsignal(signal.SIGTSTP), old_handler)
            state = StateStore(root/'state', 'task').load()
            persisted = '\n'.join(p.read_text() for p in (root/'state').iterdir() if p.is_file())
            return result, seen, commands, waiting, output.getvalue(), state, persisted

    def test_autonomous_multiple_actions_and_terminal_finish(self):
        result, seen, commands, waiting, output, state, _ = self.run_case([
            init_turn(), turn({'action':'shell','command':'one'}),
            turn({'action':'shell','command':'two'}), done('verified'),
            turn({'action':'finish','summary':'Finished.'})])
        self.assertEqual(result, 0)
        self.assertEqual(commands, ['one', 'two'])
        self.assertEqual(waiting, [])
        self.assertIn('P.A.V.L.U.S.H.A.:\nFinished.', output)
        self.assertEqual(state['run']['status'], 'finished')

    def test_pause_during_inference_discards_proposal_with_message(self):
        _, seen, commands, waiting, _, state, persisted = self.run_case([
            init_turn(), turn({'action':'shell','command':'forbidden'}),
            turn({'action':'message','text':'Acknowledged [literal].'}), done('verified'),
            turn({'action':'finish','summary':'Done'})],
            lines=('Do not modify exporter.py\n',), inference_pause=2)
        self.assertEqual(commands, [])
        self.assertEqual(waiting[0], (2, []))
        self.assertIn('Do not modify exporter.py', str(seen[2]))
        self.assertNotIn('forbidden', str(seen[2]))
        self.assertNotIn('exporter.py', str(state['project_state']))
        self.assertNotIn('\x1b[', persisted)

    def test_pause_during_inference_empty_resume_executes_proposal(self):
        _, _, commands, waiting, _, _, _ = self.run_case([
            init_turn(), turn({'action':'shell','command':'allowed'}), done('verified'),
            turn({'action':'finish','summary':'Done'})], lines=('\n',), inference_pause=2)
        self.assertEqual(waiting[0], (2, []))
        self.assertEqual(commands, ['allowed'])

    def test_shell_finishes_and_result_precedes_intervention(self):
        def composing(root, seen, commands):
            if len(seen) == 2:
                state = StateStore(root/'state', 'task').load()
                self.assertEqual(state['counters']['operation'], 1)
                self.assertEqual(commands, ['running'])
                self.assertEqual(len(seen), 2)  # No generation while input owns the boundary.
        _, seen, commands, waiting, _, _, _ = self.run_case([
            init_turn(), turn({'action':'shell','command':'running'}), done('verified'),
            turn({'action':'finish','summary':'Done'})], lines=('new constraint\n',),
            shell_pause=True, read_hook=composing)
        content = [m['content'] for m in seen[2]]
        result = next(i for i, m in enumerate(content) if m.startswith('SHELL RESULT'))
        user = next(i for i, m in enumerate(content) if m.startswith('USER MESSAGE AT'))
        self.assertLess(result, user)
        self.assertIn('completed evidence', content[result])
        self.assertEqual(commands, ['running'])
        self.assertEqual(waiting[0], (2, ['running']))

    def test_worker_wait_uses_same_input_boundary_and_reply(self):
        _, seen, commands, waiting, output, state, _ = self.run_case([
            init_turn(), turn({'action':'wait_for_user','text':'Which file?'}),
            turn({'action':'message','text':'I will use a new file.'}), done('verified'),
            turn({'action':'finish','summary':'Done'})], lines=('a new file\n',))
        self.assertEqual(waiting[0], (2, []))
        self.assertIn('P.A.V.L.U.S.H.A.:\nWhich file?', output)
        self.assertIn('USER:\na new file', output)
        self.assertIn('USER MESSAGE AT SAFE BOUNDARY:\na new file', [m['content'] for m in seen[2]])
        self.assertNotIn('a new file', str(state['project_state']))
        self.assertEqual(commands, [])
        self.assertEqual(seen[3][-1], {
            'role':'user', 'content':'MESSAGE RESULT: user-facing text displayed.'})

    def test_finish_is_terminal_and_does_not_accept_next_task(self):
        _, seen, commands, waiting, output, state, persisted = self.run_case([
            init_turn(), done('verified'), turn({'action':'finish','summary':'First complete'}),
            turn({'action':'shell','command':'must not run'})], lines=('new task\n',))
        self.assertEqual(len(seen), 3)
        self.assertEqual(commands, [])
        self.assertEqual(waiting, [])
        self.assertNotIn('PAUSED — safe to type', output)
        self.assertNotIn('new task', persisted)
        self.assertNotIn('interactive_continued', persisted)
        self.assertEqual(state['task']['original'], 'task')
        self.assertEqual(state['run']['status'], 'finished')
        self.assertEqual(state['recovery_checkpoint']['generation'], 1)

    def test_worker_wait_empty_resume_continues_until_terminal_finish(self):
        result, seen, _, waiting, _, state, _ = self.run_case([
            init_turn(), turn({'action':'wait_for_user','text':'Continue?'}),
            done('verified'), turn({'action':'finish','summary':'Done'})], lines=('\n',))
        self.assertEqual(result, 0)
        self.assertEqual(len(seen), 4)
        self.assertEqual(len(waiting), 1)
        self.assertEqual(state['run']['status'], 'finished')
        self.assertEqual(seen[2][-1], {
            'role':'user', 'content':'MESSAGE RESULT: user-facing text displayed.'})

    def test_chat_schema_available_in_all_interactive_phases_only(self):
        for flags in ({'initialized':False}, {'initialized':True,'checkpoint_required':True},
                      {'initialized':True,'periodic_review':True}, {'initialized':True}):
            for interactive in (False, True):
                schema = worker_response_format(**flags, interactive=interactive)['json_schema']['schema']
                variants = schema.get('anyOf', [schema])
                names = {v['properties']['action']['const'] for v in variants}
                self.assertEqual('message' in names, interactive)
                self.assertEqual('wait_for_user' in names, interactive)
        for bad in ({'action':'message','text':''}, {'action':'message','text':1},
                    {'action':'message','text':'x','extra':True}):
            with self.assertRaises(AgentError):
                validate_chat_action(bad)

    def test_chat_does_not_bypass_high_checkpoint(self):
        _, seen, commands, _, _, state, _ = self.run_case([
            init_turn(), done('verified'), turn({'action':'wait_for_user','text':'Need clarification'}),
            turn({'action':'project_review_complete','handoff':'Next finish'}),
            turn({'action':'finish','summary':'Done'})],
            lines=('clarification\n',), extra=('--history-high','2'))
        self.assertIn('clarification', str(seen[3]))
        self.assertIn('CURRENT RUNTIME PHASE: HIGH CHECKPOINT', str(seen[3]))
        self.assertNotIn('clarification', str(seen[4]))  # Normal checkpoint archival, Worker owns durability.
        self.assertEqual(state['recovery_checkpoint']['generation'], 2)
        self.assertEqual(commands, [])

    def test_release_restores_and_records_result_before_pause(self):
        events = []
        @contextmanager
        def released(provider):
            events.append('released')
            try:
                yield
            finally:
                events.append('restored')
        def composing(root, seen, commands):
            if len(seen) == 3:
                self.assertEqual(events, ['released','restored'])
                self.assertEqual(StateStore(root/'state','task').load()['counters']['operation'], 1)
        with patch('pavlusha_agent.provider.ChatProvider.released_worker', released):
            _, seen, commands, _, _, _, _ = self.run_case([
                init_turn(), done('verified'),
                turn({'action':'shell','command':'gpu operation','release_worker':True,'gpu':True}),
                turn({'action':'finish','summary':'Done'})],
                lines=('after restore\n',), shell_pause=True, read_hook=composing)
        self.assertIn('after restore', str(seen[3]))
        self.assertIn('completed evidence', str(seen[3]))
        self.assertEqual(commands, ['gpu operation'])

    def test_presentation_is_distinct_literal_and_ui_only(self):
        output = io.StringIO()
        with patch.dict(os.environ, {'TERM':'xterm'}, clear=True), patch.object(output, 'isatty', return_value=True):
            renderer = LiveConsoleRenderer(stream=output)
            renderer.chat_message('USER', '[bold]literal[/bold]')
            renderer.chat_message('P.A.V.L.U.S.H.A.', 'reply')
        self.assertIn('\x1b[1;96mUSER:', output.getvalue())
        self.assertIn('\x1b[1;95mP.A.V.L.U.S.H.A.:', output.getvalue())
        self.assertIn('[bold]literal[/bold]', output.getvalue())

    def test_non_tty_rejected_and_terminal_restored_after_error(self):
        with self.assertRaisesRegex(AgentError, 'requires terminal stdin'):
            with InteractiveSession(LiveConsoleRenderer(stream=io.StringIO()), stream=io.StringIO('task')):
                self.fail('non-TTY must not enter')
        master, slave = pty.openpty()
        original = termios.tcgetattr(slave)
        old = signal.getsignal(signal.SIGTSTP)
        try:
            with os.fdopen(slave, 'r') as tty:
                with self.assertRaisesRegex(RuntimeError, 'failure'):
                    with InteractiveSession(LiveConsoleRenderer(stream=io.StringIO()), stream=tty):
                        self.assertFalse(termios.tcgetattr(tty.fileno())[3] & termios.ECHO)
                        raise RuntimeError('failure')
                self.assertEqual(termios.tcgetattr(tty.fileno()), original)
                self.assertEqual(signal.getsignal(signal.SIGTSTP), old)
        finally:
            os.close(master)

    def test_real_canonical_input_unicode_and_eof(self):
        master, slave = pty.openpty()
        try:
            with os.fdopen(slave, 'r') as tty:
                session = InteractiveSession(LiveConsoleRenderer(stream=io.StringIO()), stream=tty)
                with session:
                    # Write only after PAUSED is rendered, with no timing-based synchronization.
                    def status(text):
                        if text.startswith('PAUSED'):
                            os.write(master, 'Привет\n'.encode())
                    with patch.object(session.renderer, 'chat_status', status):
                        recent = WorkingContext()
                        self.assertTrue(session.boundary(recent, force=True))
                    self.assertEqual(recent.messages()[0]['content'], 'USER MESSAGE AT SAFE BOUNDARY:\nПривет')
                    def eof(text):
                        if text.startswith('PAUSED'):
                            os.write(master, b'\x04')
                    with patch.object(session.renderer, 'chat_status', eof), self.assertRaises(SessionEnded):
                        session.boundary(recent, force=True)
        finally:
            os.close(master)

    def test_pause_during_validation_stops_before_dispatch(self):
        def validate(action, timeout):
            result = validate_action(action, timeout)
            if action.get("command") == "late proposal":
                with patch('pavlusha_agent.interactive.os.write'):
                    signal.raise_signal(signal.SIGTSTP)
            return result
        with patch('pavlusha_agent.runtime.validate_action', validate):
            _, seen, commands, waiting, _, _, _ = self.run_case([
                init_turn(), turn({'action':'shell','command':'late proposal'}),
                done('verified'), turn({'action':'finish','summary':'Done'})],
                lines=('late constraint\n',))
        self.assertEqual(commands, [])
        self.assertEqual(waiting[0], (2, []))
        self.assertIn('late constraint', str(seen[2]))
        self.assertNotIn('late proposal', str(seen[2]))

    def test_context_overflow_with_chat_fails_explicitly(self):
        with self.assertRaisesRegex(AgentError, 'without discarding or replaying user messages'):
            self.run_case([init_turn(), turn({'action':'wait_for_user','text':'Need input'}),
                           ProviderContextOverflow('context overflow')], lines=('important constraint\n',))

    def test_noninteractive_stdin_and_eof(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = build_parser().parse_args(['--workdir', str(root/'work'),
                '--state-dir', str(root/'state'), '--worker-context-budget', '40000',
                '--project-review-every', '0'])
            replies = iter([init_turn(), done('verified'), turn({'action':'finish','summary':'Done'})])
            with patch('sys.stdin', io.StringIO('piped task')), \
                 patch('pavlusha_agent.runtime.shutil.which', return_value='/fake/bwrap'), \
                 patch('pavlusha_agent.provider.ChatProvider.worker_completion', side_effect=lambda *a, **k: next(replies)), \
                 redirect_stdout(io.StringIO()):
                self.assertEqual(run_agent(args), 0)
            self.assertEqual(StateStore(root/'state','piped task').load()['task']['original'], 'piped task')
            with patch('sys.stdin', io.StringIO('')), \
                 patch('pavlusha_agent.runtime.shutil.which', return_value='/fake/bwrap'), \
                 self.assertRaisesRegex(AgentError, 'task is empty'):
                run_agent(args)

    def test_gui_helper_cannot_receive_foreground_pause_signal(self):
        with tempfile.TemporaryDirectory() as tmp:
            display = PrivateDisplay(Path(tmp))
            with patch('pavlusha_agent.gui.subprocess.run', return_value=types.SimpleNamespace(
                    returncode=0, stdout=b'{}')) as helper:
                display.action({'action':'view_gui'}, 1)
            self.assertTrue(helper.call_args.kwargs['start_new_session'])

    def test_actual_ctrl_z_requests_pause_without_suspending_process(self):
        master, slave = pty.openpty()
        notify_read, notify_write = os.pipe()
        control_read, control_write = os.pipe()
        script = r'''
import fcntl, os, signal, sys, termios
from pavlusha_agent.interactive import InteractiveSession
from pavlusha_agent.live import LiveConsoleRenderer
from pavlusha_agent.working_context import WorkingContext
os.setsid()
fcntl.ioctl(0, termios.TIOCSCTTY, 0)
notify, control = map(int, sys.argv[1:])
with InteractiveSession(LiveConsoleRenderer()) as session:
    handler = signal.getsignal(signal.SIGTSTP)
    def received(signum, frame):
        handler(signum, frame)
        os.write(notify, b"P")
    signal.signal(signal.SIGTSTP, received)
    os.write(notify, b"R")
    os.read(control, 1)
    assert session.requested and not session.paused
    session._readline = lambda: "\n"
    assert not session.boundary(WorkingContext())
    os.write(notify, b"D")
'''
        process = None
        def receive(expected):
            ready, _, _ = select.select([notify_read], [], [], 10)
            self.assertTrue(ready, 'child must acknowledge terminal event')
            self.assertEqual(os.read(notify_read, 1), expected)
        try:
            process = subprocess.Popen([sys.executable, '-c', script, str(notify_write), str(control_read)],
                stdin=slave, stdout=slave, stderr=slave, pass_fds=(notify_write, control_read))
            receive(b'R')
            os.write(master, b'\x1a')
            receive(b'P')
            os.write(control_write, b'continue')
            receive(b'D')
            self.assertEqual(process.wait(timeout=10), 0)
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()
            for fd in (master, slave, notify_read, notify_write, control_read, control_write):
                os.close(fd)

    def test_quit_or_eof_during_active_wait_ends_without_finish(self):
        for input_line in ('/quit\n', ''):
            with self.subTest(input_line=input_line):
                result, seen, commands, waiting, _, state, _ = self.run_case([
                    init_turn(), turn({'action':'wait_for_user','text':'Need a filename'}),
                    turn({'action':'shell','command':'must not run'})], lines=(input_line,))
                self.assertEqual(result, 0)
                self.assertEqual(len(seen), 2)
                self.assertEqual(commands, [])
                self.assertEqual(len(waiting), 1)
                self.assertEqual(state['run']['status'], 'running')
