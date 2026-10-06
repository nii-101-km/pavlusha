"""Small POSIX terminal pause control; semantic decisions remain with Worker."""
from __future__ import annotations

import copy
import os
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
Use {"action":"wait_for_user","text":"..."} only when information from the user is necessary.
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
        self.paused = False

    def __enter__(self):
        if not self.stream.isatty():
            raise AgentError("--interactive requires terminal stdin; use ordinary mode for piped tasks")
        self.fd = self.stream.fileno()
        self.original = termios.tcgetattr(self.fd)
        self.previous_handler = signal.getsignal(signal.SIGTSTP)
        self.running = copy.deepcopy(self.original)
        self.running[3] &= ~(termios.ECHO | termios.ECHONL)
        self.running[3] |= termios.ISIG | termios.ICANON | termios.NOFLSH
        self.running[6][termios.VSUSP] = b'\x1a'
        try:
            signal.signal(signal.SIGTSTP, self._signal_pause)
            termios.tcsetattr(self.fd, termios.TCSANOW, self.running)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        self.renderer.chat_status("Ctrl+Z: request PAUSE; type only after PAUSED. Enter: resume; /quit: end session.")
        return self

    def __exit__(self, *exc):
        try:
            termios.tcsetattr(self.fd, termios.TCSANOW, self.original)
        finally:
            signal.signal(signal.SIGTSTP, self.previous_handler)

    def _signal_pause(self, signum, frame):
        if not self.requested and not self.paused:
            self.requested = True
            # Do not re-enter Rich/TextIO while an interrupted render owns its buffer.
            os.write(2, b'\nPAUSE REQUESTED -- finishing current operation\n')

    def _readline(self):
        # Read one canonical line without TextIO prefetch surviving the next input flush.
        data = bytearray()
        while True:
            byte = os.read(self.fd, 1)
            if not byte or byte == b'\n':
                break
            data.extend(byte)
        if not byte and not data:
            return ""
        return data.decode(self.stream.encoding or "utf-8", errors="replace") + "\n"

    def boundary(self, recent, *, force=False):
        if not force and not self.requested:
            return False
        self.paused = True
        self.requested = False
        # Discard any premature typing. Input is collected ONLY after safe confirmation.
        termios.tcflush(self.fd, termios.TCIFLUSH)
        termios.tcsetattr(self.fd, termios.TCSANOW, self.original)
        try:
            self.renderer.chat_status("PAUSED — safe to type. Message + Enter; empty Enter resumes; /quit ends.")
            line = self._readline()
            if not line or line.strip() == '/quit':
                raise SessionEnded()
            text = line.rstrip('\r\n')
            if text.strip():
                self.renderer.chat_message("USER", text)
                recent.append({"role": "user", "content": "USER MESSAGE AT SAFE BOUNDARY:\n" + text},
                              kind="user_message")
            self.renderer.chat_status("RUNNING")
            return bool(text.strip())
        finally:
            termios.tcsetattr(self.fd, termios.TCSANOW, self.running)
            self.paused = False
