"""Small POSIX terminal pause control; semantic decisions remain with Worker."""
from __future__ import annotations

import copy
import os
import select
import signal
import sys
import termios

from .core import AgentError


CHAT_PROMPT = """
INTERACTIVE SESSION
Continue autonomously across turns. User interventions appear chronologically in Recent History
as USER MESSAGE AT SAFE BOUNDARY. Interpret them yourself; preserve durable facts through
Project State before checkpoint/history reset or Worker release.
You may emit {"action":"message","text":"..."} to speak to the user and continue working.
When progress is blocked and requires the user's information, access, resource, or decision,
use {"action":"wait_for_user","text":"..."}: explain the concrete obstacle and ask for the help
needed to continue. This includes conflicting mandatory requirements with no established priority.
Wait for a substantive answer; an empty resume does not resolve the blocker. Continue independently
when a justified next step can still make progress; do not ask merely because an attempt failed.
These two actions are available even during initialization or checkpoint review, but do not
complete or bypass those phases. This invocation is one atomic TASK; finish ends the runtime.
"""


class SessionEnded(Exception):
    """EOF or explicit /quit at a safe boundary."""


class RestartWorkerTurn(Exception):
    """New evidence invalidated a not-yet-dispatched generation."""


def validate_chat_action(action):
    if set(action) != {"action", "text"} or not isinstance(action.get("text"), str) or not action["text"].strip():
        raise AgentError("message/wait_for_user require exactly action and nonblank text")
    return action["action"], {"text": action["text"]}


class InteractiveSession:
    def __init__(self, renderer, stream=None):
        self.renderer = renderer
        self.stream = stream if stream is not None else sys.stdin
        self.requested = False
        self.cancel_requested = False
        self.paused = False
        self.resume_count = 0
        self.trim_history = None

    def __enter__(self):
        if not self.stream.isatty():
            raise AgentError("--interactive requires terminal stdin; use --no-interactive for piped tasks")
        self.fd = self.stream.fileno()
        self.original = termios.tcgetattr(self.fd)
        self.previous_handler = signal.getsignal(signal.SIGTSTP)
        self.previous_quit_handler = signal.getsignal(signal.SIGQUIT)
        self.running = copy.deepcopy(self.original)
        self.running[3] &= ~(termios.ECHO | termios.ECHONL)
        self.running[3] |= termios.ISIG | termios.ICANON | termios.NOFLSH
        self.running[6][termios.VSUSP] = b'\x1a'
        self.running[6][termios.VQUIT] = b'\x1c'
        try:
            signal.signal(signal.SIGTSTP, self._signal_pause)
            signal.signal(signal.SIGQUIT, self._signal_interrupt)
            termios.tcsetattr(self.fd, termios.TCSANOW, self.running)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        self.renderer.chat_status("Ctrl+Z: safe PAUSE; Ctrl+\\: cancel generation / safe PAUSE during tools. Type only after PAUSED. /trim_last_turns N: trim context; Enter: resume; /quit: end session.")
        return self

    def __exit__(self, *exc):
        try:
            termios.tcsetattr(self.fd, termios.TCSANOW, self.original)
        finally:
            signal.signal(signal.SIGTSTP, self.previous_handler)
            signal.signal(signal.SIGQUIT, self.previous_quit_handler)

    def _signal_interrupt(self, signum, frame):
        if not self.paused:
            self.requested = True
            self.cancel_requested = True
            os.write(2, b'\nCANCEL REQUESTED -- waiting for stream cancellation or safe tool boundary\n')

    def _signal_pause(self, signum, frame):
        if not self.requested and not self.paused:
            self.requested = True
            # Do not re-enter Rich/TextIO while an interrupted render owns its buffer.
            os.write(2, b'\nPAUSE REQUESTED -- finishing current operation\n')

    def _readline(self):
        # Bracketed paste keeps embedded newlines inside one message. Only an
        # Enter outside the paste submits it; signals are disabled while editing.
        data = bytearray()
        start, end = b'\x1b[200~', b'\x1b[201~'
        encoding = self.stream.encoding or "utf-8"
        while True:
            byte = os.read(self.fd, 1)
            if not byte or byte in (b'\n', b'\r'):
                break
            if byte == b'\x1b':
                sequence = byte
                while any(marker.startswith(sequence) for marker in (start, end)) and sequence not in (start, end):
                    if not select.select([self.fd], [], [], 0.2)[0]:
                        break
                    chunk = os.read(self.fd, 1)
                    if not chunk:
                        break
                    sequence += chunk
                if sequence == start:
                    pasted = bytearray()
                    while not pasted.endswith(end):
                        chunk = os.read(self.fd, 1)
                        if not chunk:
                            return ""
                        pasted.extend(chunk)
                    text = pasted[:-len(end)].decode(encoding, errors="replace")
                    text = text.replace('\r\n', '\n').replace('\r', '\n')
                    text = ''.join(c for c in text if c in '\n\t' or (ord(c) >= 32 and c != '\x7f'))
                    encoded = text.encode(encoding, errors="replace")
                    data.extend(encoded)
                    os.write(2, encoded)
                continue
            if byte == b'\x04' and not data:
                return ""
            if byte == b'\x03':
                raise KeyboardInterrupt
            if byte in (b'\x7f', b'\x08'):
                if data:
                    text = data.decode(encoding, errors="replace")
                    data[:] = text[:-1].encode(encoding, errors="replace")
                    os.write(2, b'\b \b')
            elif byte == b'\x15':  # Ctrl+U clears the current input.
                data.clear()
                os.write(2, b'\r\x1b[2K')
            elif byte == b'\t' or byte >= b' ':
                data.extend(byte)
                os.write(2, byte)
        if not byte and not data:
            return ""
        os.write(2, b'\n')
        return data.decode(encoding, errors="replace") + "\n"

    def boundary(self, recent, *, force=False, status=None, require_message=False):
        if not force and not self.requested:
            return False
        self.paused = True
        self.requested = False
        self.cancel_requested = False
        # Discard any premature typing. Input is collected ONLY after safe confirmation.
        termios.tcflush(self.fd, termios.TCIFLUSH)
        editing = copy.deepcopy(self.original)
        editing[0] &= ~(termios.IXON | termios.IXOFF | termios.ICRNL | termios.INLCR | termios.IGNCR)
        editing[3] &= ~(termios.ICANON | termios.ECHO | termios.ECHONL | termios.ISIG)
        editing[6][termios.VMIN] = 1
        editing[6][termios.VTIME] = 0
        termios.tcsetattr(self.fd, termios.TCSANOW, editing)
        os.write(2, b'\x1b[?2004h')
        try:
            self.renderer.chat_status(status or "PAUSED — safe to type. Message + Enter; empty Enter resumes; /quit ends.")
            history_changed = False
            while True:
                line = self._readline()
                if not line or line.strip() == '/quit':
                    raise SessionEnded()
                text = line.removesuffix('\n').removesuffix('\r')
                if '\n' not in text and text.split() and text.split()[0] in {'/trim_last_turns', 'trim_last_turns'}:
                    parts = text.split()
                    try:
                        if len(parts) != 2 or not parts[1].isascii() or not parts[1].isdecimal():
                            raise ValueError('Usage: /trim_last_turns N (positive integer).')
                        count = int(parts[1])
                        if self.trim_history is None:
                            raise ValueError('No active task history to trim.')
                        recent.last_turn_records(count)
                    except ValueError as exc:
                        self.renderer.chat_status(f'TRIM REFUSED — {exc}')
                        continue
                    self.renderer.chat_status(
                        '⚠️ WARNING: CONTEXT HISTORY TRIMMING\n\n'
                        'This operation is intended ONLY for removing\n'
                        'repetitive or corrupted agent turns caused by\n'
                        'reasoning/action attractors.\n\n'
                        f'The last {count} completed turns will be removed\n'
                        'from the active LLM context.\n\n'
                        'Project State, files and execution logs\n'
                        'will NOT be rolled back.\n\n'
                        'Removing valid turns may cause the agent to lose\n'
                        'important context and make incorrect decisions.\n\n'
                        'USE AT YOUR OWN RISK.\n\nConfirm trimming? [y/N]:')
                    confirmation = self._readline()
                    if not confirmation or confirmation.strip() == '/quit':
                        raise SessionEnded()
                    if confirmation.strip().lower() == 'y':
                        self.trim_history(count)
                        history_changed = True
                        self.renderer.chat_status(f'TRIMMED {count} completed turns. Still PAUSED — give guidance, Enter resumes, /quit ends.')
                    else:
                        self.renderer.chat_status('TRIM CANCELLED. Still PAUSED — give guidance, Enter resumes, /quit ends.')
                    continue
                if not require_message or text.strip():
                    break
                self.renderer.chat_status("NEED USER — provide new information; empty input keeps waiting. /quit ends.")
            if text.strip():
                self.renderer.chat_message("USER", text)
                recent.append({"role": "user", "content": "USER MESSAGE AT SAFE BOUNDARY:\n" + text},
                              kind="user_message")
            self.resume_count += 1
            self.renderer.chat_status("RUNNING")
            return bool(text.strip()) or history_changed
        finally:
            try:
                os.write(2, b'\x1b[?2004l')
            finally:
                termios.tcsetattr(self.fd, termios.TCSANOW, self.running)
                self.paused = False
