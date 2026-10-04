"""One structurally validated, self-contained committed recovery generation.

The working state and checkpoint envelope share one atomically replaced state.json.
Initialization, true HIGH completion and intentional Worker release replace the envelope.
All use the same atomic transaction; no reconstruction.
"""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime
from typing import Any

from .config import STATE_SCHEMA_VERSION
from .core import AgentError, _task_hash, _now
from .project_state import validate_handoff, validate_persisted_project_state

CHECKPOINT_SCHEMA_VERSION = 1


def _integer(value: Any, field: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise AgentError(f"checkpoint {field} must be an integer >= {minimum}")
    return value


def _digest(payload: dict[str, Any]) -> str:
    try:
        data = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise AgentError("checkpoint must be finite JSON with valid UTF-8 text") from exc
    return hashlib.sha256(data).hexdigest()


def validate_checkpoint(state: dict[str, Any], task: str) -> dict[str, Any]:
    """Return the existing envelope or fail; never fill in missing recovery fields."""
    if type(state.get("schema_version")) is not int or state["schema_version"] != STATE_SCHEMA_VERSION:
        raise AgentError("unsupported controller state schema")
    task_record = state.get("task")
    if (not isinstance(task_record, dict) or task_record.get("original") != task
            or task_record.get("sha256") != _task_hash(task) or task_record.get("immutable") is not True):
        raise AgentError("controller task identity does not match the resumed task")
    version = _integer(state.get("version"), "controller version")
    checkpoint = state.get("recovery_checkpoint")
    if not isinstance(checkpoint, dict):
        raise AgentError("no committed recovery generation (legacy/working state is not a checkpoint)")
    required = {"schema_version", "generation", "kind", "project_state", "handoff",
                "state_version", "operation_count", "step", "task_sha256", "committed_at", "sha256"}
    if set(checkpoint) != required:
        raise AgentError("incomplete or unsupported recovery generation fields")
    if (type(checkpoint["schema_version"]) is not int
            or checkpoint["schema_version"] != CHECKPOINT_SCHEMA_VERSION):
        raise AgentError("unsupported checkpoint schema")
    generation = _integer(checkpoint["generation"], "generation", 1)
    _integer(checkpoint["step"], "step", 1)
    committed_version = _integer(checkpoint["state_version"], "state_version", 1)
    operations = _integer(checkpoint["operation_count"], "operation_count")
    if committed_version > version or generation > committed_version:
        raise AgentError("checkpoint version exceeds controller state version")
    if checkpoint["kind"] not in ("initial", "high") or checkpoint["task_sha256"] != _task_hash(task):
        raise AgentError("checkpoint kind/task identity is invalid")
    if not isinstance(checkpoint["committed_at"], str) or not checkpoint["committed_at"]:
        raise AgentError("checkpoint requires commit timestamp")
    try:
        timestamp = datetime.fromisoformat(checkpoint["committed_at"])
    except ValueError as exc:
        raise AgentError("checkpoint commit timestamp must be ISO 8601") from exc
    if timestamp.tzinfo is None:
        raise AgentError("checkpoint commit timestamp requires timezone")
    if (checkpoint["kind"] == "initial") != (generation == 1):
        raise AgentError("checkpoint generation/kind mismatch")
    project = checkpoint["project_state"]
    validate_persisted_project_state(project)
    if project["revision"] < generation:
        raise AgentError("checkpoint generation exceeds Project State revision")
    if project["last_review_operation"] != operations:
        raise AgentError("checkpoint operation metadata does not match Project State")
    if validate_handoff(checkpoint["handoff"]) != checkpoint["handoff"]:
        raise AgentError("checkpoint handoff must be normalized text")
    payload = {k: v for k, v in checkpoint.items() if k != "sha256"}
    if checkpoint["sha256"] != _digest(payload):
        raise AgentError("checkpoint integrity mismatch (State/handoff/metadata must be one generation)")
    counters = state.get("counters")
    if not isinstance(counters, dict):
        raise AgentError("controller operation counter missing")
    current_operations = _integer(counters.get("operation"), "controller operation counter")
    ledger = state.get("operations")
    if not isinstance(ledger, list) or len(ledger) != current_operations or operations > current_operations:
        raise AgentError("controller operation ledger/counter is inconsistent")
    for index, entry in enumerate(ledger, 1):
        if not isinstance(entry, dict) or entry.get("id") != f"OP{index:04d}":
            raise AgentError("controller operation ledger IDs are inconsistent")
    if not isinstance(state.get("metadata"), dict) or not isinstance(state.get("run"), dict):
        raise AgentError("controller runtime metadata missing")
    return copy.deepcopy(checkpoint)


def prepare_checkpoint(state: dict[str, Any], *, kind: str, step: int, handoff: str) -> None:
    """Build and validate privately; caller publishes the complete containing image."""
    task = state["task"]["original"]
    previous = validate_checkpoint(state, task) if "recovery_checkpoint" in state else None
    payload = {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "generation": previous["generation"] + 1 if previous else 1,
        "kind": kind,
        "project_state": copy.deepcopy(state["project_state"]),
        "handoff": validate_handoff(handoff),
        "state_version": state["version"],
        "operation_count": state["counters"]["operation"],
        "step": step,
        "task_sha256": state["task"]["sha256"],
        "committed_at": _now(),
    }
    state["recovery_checkpoint"] = {**payload, "sha256": _digest(payload)}
    validate_checkpoint(state, task)
