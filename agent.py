#!/usr/bin/env python3
"""Compatibility facade for the modular Pavlusha implementation.

The implementation lives in ``pavlusha_agent``.  This file intentionally keeps
historical imports and ``python agent.py ...`` working while allowing each
subsystem to be inspected independently.
"""
from __future__ import annotations

# Kept public for compatibility with existing tests/callers that monkeypatch
# urllib.request via the old agent module.
import urllib.error
import urllib.request
from pathlib import Path

from pavlusha_agent.config import *
from pavlusha_agent.config import _SOURCE_PROPERTY, _STATUS_PROPERTY, _empty_state_patch, _tool_function
from pavlusha_agent.core import *
from pavlusha_agent.core import _extract_json_object, _normalized, _now, _task_hash, _trim
from pavlusha_agent.provider import ChatProvider
from pavlusha_agent.experiment import ExperimentRecorder, context_meter_message
from pavlusha_agent.working_context import (
    ContextItem, ContextReplacement, WorkingContext, context_inventory_message, context_pressure_level, context_pressure_message,
    context_maintenance_transition, context_maintenance_gate_message, context_maintenance_action_allowed
)
from pavlusha_agent.state_store import StateStore
from pavlusha_agent.project_state import *
from pavlusha_agent.state_cycle import (
    _decode_state_tool_call,
    _maybe_run_state_cycle,
    _state_for_manager,
    _state_for_worker,
    _state_message,
    _state_patch_from_tool_calls,
    _transcript_text,
    _worker_context_pressure_reason,
    _worker_context_threshold,
    _worker_reasoning_attractor_reason,
    _working_thoughts_message,
    assimilate_state,
    propose_state_patch,
    run_state_cycle,
)
from pavlusha_agent import sandbox as _sandbox

_sandbox_existing_system_paths = _sandbox._existing_system_paths
from pavlusha_agent.runtime import _default_state_dir, run_agent
from pavlusha_agent.project_map import ProjectMap, ProjectMapRefresh, ProjectSymbol, PythonTreeSitterIndexer
from pavlusha_agent.cli import build_parser, main, run_bwrap_self_test


def _existing_system_paths() -> list[str]:
    """Compatibility hook; callers may still monkeypatch this old symbol."""
    return _sandbox_existing_system_paths()


def build_bwrap_command(workdir: Path, shell_command: str, *, network: bool) -> list[str]:
    """Compatibility wrapper preserving the old monkeypatch surface."""
    original = _sandbox._existing_system_paths
    _sandbox._existing_system_paths = _existing_system_paths
    try:
        return _sandbox.build_bwrap_command(workdir, shell_command, network=network)
    finally:
        _sandbox._existing_system_paths = original


run_shell = _sandbox.run_shell
validate_action = _sandbox.validate_action


if __name__ == "__main__":
    raise SystemExit(main())
