"""Shared result types, errors, and small serialization helpers."""

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

class AgentError(RuntimeError):
    pass


@dataclass
class ShellResult:
    command: str
    network: bool
    exit_code: int | None
    timed_out: bool
    stdout: str
    stderr: str
    duration: float
    stdout_chars: int | None = None
    stderr_chars: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "network": self.network,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "duration_seconds": round(self.duration, 3),
            "stdout": self.stdout,
            "stderr": self.stderr,
            "stdout_chars": self.stdout_chars if self.stdout_chars is not None else len(self.stdout),
            "stderr_chars": self.stderr_chars if self.stderr_chars is not None else len(self.stderr),
        }


@dataclass
class ProviderTurn:
    content: str
    reasoning_content: str
    finish_reason: str | None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    reasoning_tokens: int | None = None
    tool_calls: list[dict[str, Any]] | None = None


@dataclass
class StateCycleResult:
    state: dict[str, Any]
    processed_iterations: list[str]
    failed_iteration: str | None
    quarantined_iterations: list[str]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _trim(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    keep = max(1, limit // 2)
    omitted = len(text) - 2 * keep
    return text[:keep] + f"\n... <{omitted} chars omitted> ...\n" + text[-keep:]


def _extract_json_object(text: str) -> dict[str, Any]:
    """Accept clean JSON, fenced JSON, or a JSON object surrounded by model chatter."""
    candidate = text.strip()
    if candidate.startswith("```") and candidate.endswith("```"):
        lines = candidate.splitlines()
        if len(lines) >= 3:
            candidate = "\n".join(lines[1:-1]).strip()
    try:
        value = json.loads(candidate)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    for index, char in enumerate(candidate):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(candidate[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise AgentError("model did not return a JSON object")


def _normalized(text: str) -> str:
    return " ".join(text.split()).casefold()


def _task_hash(task: str) -> str:
    return hashlib.sha256(task.encode("utf-8")).hexdigest()
