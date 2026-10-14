"""Exercise the actual terminal reader, not a mocked _readline."""
import json
import os
import pty
import select
import subprocess
import sys
import time
import unittest


SCRIPT = '''
import json, termios, types
from pavlusha_agent.interactive import InteractiveSession, SessionEnded
original = termios.tcgetattr(0)
messages = []
renderer = types.SimpleNamespace(chat_status=lambda s: print(s, flush=True),
                                 chat_message=lambda *args: None)
recent = types.SimpleNamespace(append=lambda m, **kw: messages.append(m))
try:
    with InteractiveSession(renderer) as session:
        session.boundary(recent, force=True)
except SessionEnded:
    pass
except KeyboardInterrupt:
    pass
print('RESULT:' + json.dumps({'messages': messages,
    'restored': termios.tcgetattr(0) == original}), flush=True)
'''


class TerminalPasteTests(unittest.TestCase):
    def start_reader(self):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        proc = subprocess.Popen([sys.executable, '-c', SCRIPT], stdin=slave,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        os.close(slave)
        self.addCleanup(lambda: proc.kill() if proc.poll() is None else None)
        for _ in range(3):
            self.assertTrue(select.select([proc.stdout], [], [], 5)[0])
            if proc.stdout.readline().startswith(b'PAUSED'):
                return proc, master
        self.fail('reader did not reach safe input')

    def result(self, proc):
        out, err = proc.communicate(timeout=5)
        self.assertEqual(proc.returncode, 0, err.decode(errors='replace'))
        line = next(line for line in out.decode().splitlines() if line.startswith('RESULT:'))
        result = json.loads(line[len('RESULT:'):])
        self.assertTrue(result['restored'])
        return result['messages']

    def test_fragmented_multiline_paste_waits_for_separate_enter(self):
        proc, master = self.start_reader()
        text = 'REJECTED: проблема\n\nОписание дефекта\n/quit\n'
        packet = b'\x1b[200~' + text.encode() + b'\x1b[201~'
        for part in [packet[:3], packet[3:12], packet[12:-3], packet[-3:]]:
            os.write(master, part)
            time.sleep(.01)
        self.assertFalse(select.select([proc.stdout], [], [], .1)[0],
                         'pasted newline or end marker submitted the message')
        os.write(master, b'\r')
        messages = self.result(proc)
        self.assertEqual(messages, [{'role': 'user',
            'content': 'USER MESSAGE AT SAFE BOUNDARY:\n' + text}])

    def test_pasted_control_bytes_do_not_signal_or_control_flow(self):
        proc, master = self.start_reader()
        os.write(master, b'\x1b[200~before\x1a\x1c\x03\x13\x11\x04\r\nafter\x1b[201~')
        self.assertFalse(select.select([proc.stdout], [], [], .1)[0])
        os.write(master, b'\r')
        self.assertEqual(self.result(proc)[0]['content'],
                         'USER MESSAGE AT SAFE BOUNDARY:\nbefore\nafter')

    def test_typed_utf8_backspace_and_submit(self):
        proc, master = self.start_reader()
        os.write(master, 'даб'.encode() + b'\x7f\r')
        self.assertEqual(self.result(proc)[0]['content'], 'USER MESSAGE AT SAFE BOUNDARY:\nда')

    def test_empty_enter_quit_eof_and_interrupt_restore_terminal(self):
        for data in [b'\r', b'/quit\r', b'\x04', b'\x03']:
            with self.subTest(data=data):
                proc, master = self.start_reader()
                os.write(master, data)
                self.assertEqual(self.result(proc), [])
