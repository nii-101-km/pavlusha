"""Bubblewrap command construction, shell execution, and worker action validation."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

from .core import AgentError, ShellResult

def _existing_system_paths() -> list[str]:
    # Deliberately do not bind /home, /root, /mnt, /media, /run or the host cwd.
    candidates = ["/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc"]
    return [path for path in candidates if os.path.exists(path)]


def _nvidia_compute_devices() -> list[Path]:
    """Only compute nodes, freshly discovered; never mount the host /dev directory."""
    gpus = sorted(path for path in Path("/dev").glob("nvidia[0-9]*")
                  if path.name[6:].isascii() and path.name[6:].isdigit())
    if not gpus:
        raise OSError("GPU access requested but no numbered NVIDIA device nodes are present")
    devices = [Path("/dev/nvidiactl"), Path("/dev/nvidia-uvm"), *gpus]
    for path in devices:
        if path.is_symlink() or not path.is_char_device():
            raise OSError(f"GPU access requires a host NVIDIA character device: {path}")
    return devices


def build_bwrap_command(
    workdir: Path,
    shell_command: str,
    *,
    network: bool,
    gpu: bool = False,
) -> list[str]:
    args = [
        "bwrap",
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
    ]
    if network:
        args.append("--share-net")

    for path in _existing_system_paths():
        args += ["--ro-bind", path, path]

    if network:
        # /etc is read-only, but resolv.conf may point outside it into omitted /run.
        # Share-net includes host loopback, so its stub resolver remains reachable.
        # Bind only the resolved regular file, freshly resolved for each command;
        # never expose the host /run directory or its service sockets.
        resolver = Path("/etc/resolv.conf")
        target = resolver.resolve(strict=True)
        if not target.is_file():
            raise OSError("host /etc/resolv.conf does not resolve to a regular file")
        if target != resolver:
            args += ["--ro-bind", str(target), str(target)]

    args += [
        "--proc", "/proc",
        "--dev", "/dev",
    ]
    if gpu:
        for path in _nvidia_compute_devices():
            args += ["--dev-bind", str(path), str(path)]
    args += [
        "--tmpfs", "/tmp",
        "--bind", str(workdir), "/work",
        "--chdir", "/work",
        "--clearenv",
        "--setenv", "HOME", "/work",
        "--setenv", "USER", "worker",
        "--setenv", "LOGNAME", "worker",
        "--setenv", "PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "--setenv", "TMPDIR", "/tmp",
        "--setenv", "XDG_CACHE_HOME", "/work/.cache",
        "--setenv", "PIP_CACHE_DIR", "/work/.cache/pip",
        "--setenv", "NPM_CONFIG_CACHE", "/work/.cache/npm",
        "--setenv", "DEBIAN_FRONTEND", "noninteractive",
        "/bin/bash", "--noprofile", "--norc", "-lc", shell_command,
    ]
    return args


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
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = process.communicate(timeout=0.5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate()
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
