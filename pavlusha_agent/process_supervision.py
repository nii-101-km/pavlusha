"""Bounded cleanup of controller-owned process sessions; no external process discovery."""
from contextlib import contextmanager
import os
import signal
import subprocess
import sys
import threading

from .core import ProcessCleanupError


def _signal_group(process, signum):
    try:
        os.killpg(process.pid, signum)
    except ProcessLookupError:
        pass
    except OSError as exc:
        raise ProcessCleanupError('Cannot signal owned process; Worker restore blocked') from exc


def stop_owned_process(process):
    """Reap the namespace supervisor before returning; fail closed if it cannot die."""
    if process.poll() is not None:
        return process.communicate(timeout=5)
    _signal_group(process, signal.SIGTERM)
    try:
        return process.communicate(timeout=.5)
    except subprocess.TimeoutExpired:
        _signal_group(process, signal.SIGKILL)
        try:
            return process.communicate(timeout=5)
        except BaseException as exc:
            raise ProcessCleanupError('Owned process cleanup unconfirmed; Worker restore blocked') from exc
    except BaseException as exc:
        # An interrupted/broken pipe read must still kill and reap the supervisor.
        _signal_group(process, signal.SIGKILL)
        try:
            process.wait(timeout=5)
        except BaseException as cleanup:
            raise ProcessCleanupError('Owned process cleanup unconfirmed; Worker restore blocked') from cleanup
        raise exc


@contextmanager
def termination_signals():
    """Turn terminal closure and termination into normal Python cleanup on the main thread."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    handled = (signal.SIGHUP, signal.SIGTERM)
    previous = {sig: signal.getsignal(sig) for sig in handled}
    def interrupt(signum, frame):
        # A second hangup/termination must not interrupt resource cleanup.
        for sig in handled:
            signal.signal(sig, signal.SIG_IGN)
        raise KeyboardInterrupt
    try:
        for sig in handled:
            signal.signal(sig, interrupt)
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def report_interrupted():
    """Report cancellation when possible, even if the terminal has disappeared."""
    try:
        print('Interrupted.', file=sys.stderr, flush=True)
    except OSError:
        pass
    # A disconnected terminal can also make Python's final stream flush fail.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except OSError:
            try:
                fd = os.open(os.devnull, os.O_WRONLY)
                try:
                    os.dup2(fd, stream.fileno())
                finally:
                    os.close(fd)
            except (OSError, ValueError):
                pass
