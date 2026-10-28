"""Bounded cleanup of controller-owned process sessions; no external process discovery."""
import os
import signal
import subprocess

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
