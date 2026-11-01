"""Current runtime defaults and Worker instructions."""

from __future__ import annotations

DEFAULT_MAX_REASONING_RECOVERIES = 3
DEFAULT_PROJECT_MAP = "on"
DEFAULT_PROJECT_REVIEW_EVERY = 10
STATE_SCHEMA_VERSION = 3

SYSTEM_PROMPT = """You are an autonomous assistant. Follow the task and available module instructions.
Reply with exactly one complete JSON action from the current response schema.
Use observations and tool results as evidence; do not claim an unverified outcome.
""".strip()


def build_worker_system_prompt() -> str:
    return SYSTEM_PROMPT
