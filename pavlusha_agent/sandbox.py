"""Bubblewrap command construction, shell execution, and worker action validation."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .core import AgentError, ShellResult, _trim

def _existing_system_paths() -> list[str]:
    # Deliberately do not bind /home, /root, /mnt, /media, /run or the host cwd.
    candidates = ["/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc"]
    return [path for path in candidates if os.path.exists(path)]


def build_bwrap_command(
    workdir: Path,
    shell_command: str,
    *,
    network: bool,
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
    output_limit: int,
) -> ShellResult:
    argv = build_bwrap_command(workdir, command, network=network)
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
    *,
    allow_context_drop: bool = False,
    available_context_ids: set[str] | None = None,
    available_context_tombstone_limits: dict[str, int] | None = None,
    available_context_summary_limits: dict[str, int] | None = None,
) -> tuple[str, dict[str, Any]]:
    kind = action.get("action")
    if kind == "finish":
        summary = action.get("summary", "")
        if not isinstance(summary, str) or not summary.strip():
            raise AgentError("finish action requires a non-empty summary")
        return kind, {"summary": summary.strip()}

    if kind == "drop_context":
        if not allow_context_drop:
            raise AgentError("drop_context is not enabled for this Worker")
        raw_items = action.get("items")
        normalized_items: list[dict[str, str]] = []
        legacy_intent = ""
        if raw_items is not None:
            if not isinstance(raw_items, list) or not raw_items or len(raw_items) > 32:
                raise AgentError("drop_context.items must be a non-empty list of at most 32 replacements")
            for item in raw_items:
                if not isinstance(item, dict):
                    raise AgentError("drop_context.items entries must be objects")
                handle = item.get("id")
                reason = item.get("reason")
                if not isinstance(handle, str) or not handle.strip():
                    raise AgentError("drop_context item.id must be a non-empty handle")
                if not isinstance(reason, str) or not reason.strip():
                    raise AgentError("drop_context item.reason must be a non-empty string")
                reason = reason.strip()
                normalized_items.append({"id": handle.strip(), "reason": reason})
        else:
            # Backward-compatible legacy shape used by earlier benches/callers.
            ids = action.get("ids")
            intent = action.get("intent")
            if (
                not isinstance(ids, list)
                or not ids
                or len(ids) > 32
                or any(not isinstance(item, str) or not item.strip() for item in ids)
            ):
                raise AgentError("drop_context.ids must be a non-empty list of at most 32 handles")
            if not isinstance(intent, str) or not intent.strip():
                raise AgentError("drop_context.intent must be a non-empty string")
            legacy_intent = intent.strip()
            normalized_items = [
                {"id": str(item).strip(), "reason": legacy_intent} for item in ids
            ]

        normalized = [item["id"] for item in normalized_items]
        if len(set(normalized)) != len(normalized):
            raise AgentError("drop_context handle IDs must not contain duplicates")
        if available_context_ids is not None:
            unknown = [item for item in normalized if item not in available_context_ids]
            if unknown:
                raise AgentError(
                    "drop_context references unavailable or non-removable handle(s): "
                    + ", ".join(unknown)
                )
        if available_context_tombstone_limits is not None:
            for item in normalized_items:
                handle = item["id"]
                limit = int(available_context_tombstone_limits.get(handle, 0) or 0)
                if limit <= 0:
                    raise AgentError(
                        f"drop_context cannot reduce {handle}; leave the tiny raw item protected"
                    )
                if len(item["reason"]) > limit:
                    raise AgentError(
                        f"drop_context reason for {handle} must be at most {limit} characters "
                        "for this raw item"
                    )
        return kind, {"items": normalized_items, "ids": normalized, "intent": legacy_intent}

    if kind == "compact_context":
        if not allow_context_drop:
            raise AgentError("compact_context is not enabled for this Worker")
        items = action.get("items")
        if not isinstance(items, list) or not items or len(items) > 16:
            raise AgentError("compact_context.items must be a non-empty list of at most 16 replacements")
        normalized_items: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                raise AgentError("compact_context.items entries must be objects")
            handle = item.get("id")
            summary = item.get("summary")
            if not isinstance(handle, str) or not handle.strip():
                raise AgentError("compact_context item.id must be a non-empty handle")
            handle = handle.strip()
            if handle in seen:
                raise AgentError("compact_context handle IDs must not contain duplicates")
            seen.add(handle)
            if available_context_ids is not None and handle not in available_context_ids:
                raise AgentError(
                    "compact_context references unavailable or non-removable handle(s): " + handle
                )
            if not isinstance(summary, str) or not summary.strip():
                raise AgentError(f"compact_context summary for {handle} must be non-empty")
            summary = summary.strip()
            if available_context_summary_limits is not None:
                limit = int(available_context_summary_limits.get(handle, 0) or 0)
                if limit <= 0:
                    raise AgentError(
                        f"compact_context cannot reduce {handle}; use drop_context if it is obsolete"
                    )
                if len(summary) > limit:
                    raise AgentError(
                        f"compact_context summary for {handle} is {len(summary)} characters; "
                        f"maximum is {limit} for this raw item"
                    )
            normalized_items.append({"id": handle, "summary": summary})
        return kind, {"items": normalized_items}

    if kind != "shell":
        expected = (
            "'shell', 'drop_context', 'compact_context', or 'finish'"
            if allow_context_drop else "'shell' or 'finish'"
        )
        raise AgentError(f"unknown action {kind!r}; expected {expected}")

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
    return kind, {"command": command, "network": network, "timeout": timeout}
