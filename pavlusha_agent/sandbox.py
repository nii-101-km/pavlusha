"""Bubblewrap command construction, shell execution, and worker action validation."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

from .core import AgentError, ShellResult
from .process_supervision import stop_owned_process

from pavlusha_runtime.sandbox import build_bwrap_command


def run_shell(
    workdir: Path,
    command: str,
    *,
    network: bool,
    timeout: int,
    gpu: bool = False,
) -> ShellResult:
    """Run a bounded sandbox command and return its output before admission checks."""
    argv = build_bwrap_command(workdir, command, network=network, gpu=gpu)
    started = time.monotonic()
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        start_new_session=True,
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        stdout, stderr = stop_owned_process(process)
    except BaseException:
        stop_owned_process(process)
        raise
    duration = time.monotonic() - started
    return ShellResult(
        command=command,
        network=network,
        exit_code=process.returncode,
        timed_out=timed_out,
        stdout=stdout,
        stderr=stderr,
        duration=duration,
        stdout_chars=len(stdout),
        stderr_chars=len(stderr),
    )


def admit_shell_result(result: ShellResult, output_limit: int) -> dict[str, Any]:
    """Admit a shell result to Worker context only when its output fits the hard door.

    The command has already executed. Oversized stdout/stderr is withheld wholesale rather
    than truncated or summarized, so the Worker must issue a narrower evidence query.
    """
    payload = result.as_dict()
    stdout_chars = int(payload["stdout_chars"])
    stderr_chars = int(payload["stderr_chars"])
    total_chars = stdout_chars + stderr_chars
    if total_chars <= output_limit:
        return payload
    payload["stdout"] = ""
    payload["stderr"] = ""
    payload["output_withheld"] = True
    payload["output_chars"] = total_chars
    payload["output_limit_chars"] = output_limit
    payload["output_notice"] = (
        "OUTPUT_WITHHELD: command completed, but its output exceeded the Core admission limit; "
        "full stdout/stderr was not admitted to Worker context. Use Project Map and a narrower "
        "query (targeted file/range, grep, head, or tail)."
    )
    return payload


def validate_action(
    action: dict[str, Any],
    max_command_timeout: int,
) -> tuple[str, dict[str, Any]]:
    kind = action.get("action")
    if kind == "finish":
        summary = action.get("summary", "")
        if not isinstance(summary, str) or not summary.strip():
            raise AgentError("finish action requires a non-empty summary")
        return kind, {"summary": summary.strip()}

    if kind != "shell":
        raise AgentError(f"unknown action {kind!r}; expected 'shell' or 'finish'")

    command = action.get("command")
    if not isinstance(command, str) or not command.strip():
        raise AgentError("shell action requires a non-empty command")
    network = action.get("network", False)
    if not isinstance(network, bool):
        raise AgentError("shell.network must be true or false")
    timeout = action.get("timeout", min(120, max_command_timeout))
    if not isinstance(timeout, int) or isinstance(timeout, bool):
        raise AgentError("shell.timeout must be an integer")
    timeout = max(1, min(timeout, max_command_timeout))
    release = action.get("release_worker", False)
    if not isinstance(release, bool):
        raise AgentError("shell.release_worker must be true or false")
    gpu = action.get("gpu", False)
    if not isinstance(gpu, bool):
        raise AgentError("shell.gpu must be true or false")
    data = {"command": command, "network": network, "timeout": timeout}
    if release:
        data["release_worker"] = True
    if gpu:
        data["gpu"] = True
    return kind, data
