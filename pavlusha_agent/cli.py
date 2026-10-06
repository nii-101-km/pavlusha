"""Command-line interface and bubblewrap self-test."""

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

from .config import (
    DEFAULT_MAX_REASONING_RECOVERIES,
    DEFAULT_STATE_AFTER,
    DEFAULT_STATE_API_TIMEOUT,
    DEFAULT_STATE_CYCLE_AFTER,
    DEFAULT_STATE_CYCLE_CONTEXT_RATIO,
    DEFAULT_CONTEXT_METER_MODE,
    DEFAULT_WORKER_CONTEXT_CONTROL,
    DEFAULT_PROJECT_MAP,
    DEFAULT_CONTEXT_PRESSURE_GUIDANCE,
    DEFAULT_CONTEXT_PRESSURE_SOFT_RATIO,
    DEFAULT_CONTEXT_PRESSURE_STRONG_RATIO,
    DEFAULT_CONTEXT_PRESSURE_URGENT_RATIO,
    DEFAULT_CONTEXT_MAINTENANCE_GATE,
    DEFAULT_CONTEXT_MAINTENANCE_BUDGET,
    DEFAULT_CONTEXT_MAINTENANCE_ENTER_RATIO,
    DEFAULT_CONTEXT_MAINTENANCE_RELEASE_RATIO,
    DEFAULT_STATE_KEEP,
    DEFAULT_STATE_MAX_TOKENS,
    DEFAULT_STATE_REASONING_BUDGET,
    DEFAULT_RAW_REASONING_LIMIT,
    DEFAULT_PROJECT_REVIEW_EVERY,
)
from .core import AgentError
from .runtime import run_agent
from .sandbox import run_shell

def run_bwrap_self_test(workdir_text: str) -> int:
    if shutil.which("bwrap") is None:
        raise AgentError("bubblewrap is not installed (expected executable: bwrap)")
    workdir = Path(workdir_text).expanduser().resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    probe = (
        "set -eu; "
        "test \"$PWD\" = /work; "
        "test \"$HOME\" = /work; "
        "test -z \"${AGENT_API_KEY:-}\"; "
        "test ! -e /home; "
        "if touch /etc/pavlusha-bwrap-write-test 2>/dev/null; then exit 41; fi; "
        "printf 'sandbox-ok\\n' > .bwrap-self-test.txt; "
        "cat .bwrap-self-test.txt"
    )
    result = run_shell(workdir, probe, network=False, timeout=20, output_limit=8000)
    if result.exit_code != 0 or result.timed_out or "sandbox-ok" not in result.stdout:
        raise AgentError(
            "bubblewrap self-test failed: "
            + json.dumps(result.as_dict(), ensure_ascii=False)
        )
    print("bubblewrap self-test: OK")
    print(f"workdir: {workdir}")
    print("verified: /work writable, /etc read-only, /home absent, API key env cleared, network namespace private")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Autonomous LLM worker in bubblewrap with controller-owned persistent state."
    )
    parser.add_argument("task", nargs="*", help="task text; if omitted, read it from stdin")
    parser.add_argument("--workdir", default="./agent-work", help="persistent writable working directory")
    parser.add_argument("--network", action="store_true", help="allow the model to request networked shell commands")
    parser.add_argument(
        "--gui", action="store_true",
        help="enable private 800x600 Xvfb GUI actions (requires Xvfb, python-xlib and Pillow)",
    )
    parser.add_argument("--self-test-bwrap", action="store_true", help="test the local bubblewrap isolation and exit")
    parser.add_argument("--base-url", default=os.getenv("AGENT_BASE_URL", "http://127.0.0.1:1234/v1"))
    parser.add_argument("--model", default=os.getenv("AGENT_MODEL") or None)
    parser.add_argument("--api-key", default=os.getenv("AGENT_API_KEY", ""))
    parser.add_argument("--api-timeout", type=float, default=300.0)
    parser.add_argument("--expert", choices=("off", "on"), default="off")
    parser.add_argument("--expert-transport", choices=("chat-completions", "perplexity-agent"), default="chat-completions",
                        help="Expert wire protocol; does not change Worker configuration")
    parser.add_argument("--expert-base-url", help="independent Expert API URL; required when enabled")
    parser.add_argument("--expert-model", help="independent consultant model; required when enabled")
    parser.add_argument("--expert-api-key-env", default="EXPERT_API_KEY", help="environment variable containing the Expert API key")
    parser.add_argument("--expert-max-tokens", type=int, default=4096)
    parser.add_argument("--expert-reasoning-effort", help="optional provider reasoning_effort; unsupported values return a tool error")
    parser.add_argument("--expert-max-calls", type=int, default=5, help="attempt ceiling per controller run; failures count")
    parser.add_argument("--expert-timeout", type=float, default=60.0, help="Expert HTTP timeout in seconds")
    parser.add_argument(
        "--state-api-timeout",
        type=float,
        default=DEFAULT_STATE_API_TIMEOUT,
        help="State Manager HTTP timeout; reasoning itself is separately bounded",
    )
    parser.add_argument("--reasoning-loop-recovery", choices=("off", "observe", "recover"),
                        default="off", help="streamed lexical loop detection; off preserves prior behavior")
    parser.add_argument("--max-reasoning-loop-recoveries", type=int, default=3,
                        help="maximum consecutive retry generations after a lexical loop (default: 3)")
    parser.add_argument("--max-steps", type=int, default=60, help="Worker step limit; -1 means unlimited")
    parser.add_argument(
        "--state-dir",
        help="controller-owned persistent state directory; default: <workdir>.pavlusha-state",
    )
    parser.add_argument("--reset-state", action="store_true", help="discard existing persistent state for this state-dir")
    parser.add_argument(
        "--state-after", "--compact-after",
        dest="state_after", type=int, default=DEFAULT_STATE_AFTER,
        help="fallback raw-message pressure limit when provider does not report prompt_tokens (legacy alias: --compact-after)",
    )
    parser.add_argument(
        "--worker-context-budget", "--context-budget",
        dest="worker_context_budget", type=int, default=None,
        help="override Worker context budget; by default discover active LM Studio context_length",
    )
    parser.add_argument(
        "--state-cycle-context-ratio", type=float, default=DEFAULT_STATE_CYCLE_CONTEXT_RATIO,
        help="run a global state cycle when Worker prompt usage reaches this fraction of context budget",
    )
    parser.add_argument(
        "--state-cycle-token-limit", type=int, default=None,
        help="explicit Worker prompt-token threshold for semantic compaction; overrides --state-cycle-context-ratio",
    )
    parser.add_argument(
        "--context-meter", choices=("hidden", "visible"), default=DEFAULT_CONTEXT_METER_MODE,
        help="hidden logs context usage only; visible also shows previous prompt-token usage to the Worker",
    )
    parser.add_argument(
        "--project-map", choices=("off", "on"), default=DEFAULT_PROJECT_MAP,
        help=(
            "on supplies a frozen checkpoint snapshot of the Python project map; "
            "file SHA-256 values are cached outside /work and only changed files are reparsed"
        ),
    )
    parser.add_argument(
        "--history-window", choices=("off", "on"), default="off",
        help="optional batch eviction of oldest complete Worker steps; no summarization",
    )
    parser.add_argument("--history-context-high", type=float, default=0.85,
                        help="HIGH fraction of provider-reported prompt usage / model capacity (default: 0.85)")
    parser.add_argument("--history-high", type=int, default=30,
                        help="require a fresh checkpoint and full history reset at this many retained Worker steps (default: 30)")
    parser.add_argument(
        "--worker-context-control", choices=("off", "drop"), default=DEFAULT_WORKER_CONTEXT_CONTROL,
        help=(
            "off keeps raw Worker context controller-managed; drop enables selective in-place "
            "tombstone/summary replacement of listed raw W-handles"
        ),
    )
    parser.add_argument(
        "--context-pressure-guidance", choices=("off", "progressive"),
        default=DEFAULT_CONTEXT_PRESSURE_GUIDANCE,
        help=(
            "off keeps context-control prompting neutral; progressive adds one ephemeral final "
            "current-turn message with pressure-aware context-replacement guidance"
        ),
    )
    parser.add_argument(
        "--context-pressure-soft-ratio", type=float, default=DEFAULT_CONTEXT_PRESSURE_SOFT_RATIO,
        help="fraction of semantic-compaction threshold where soft drop_context guidance begins",
    )
    parser.add_argument(
        "--context-pressure-strong-ratio", type=float, default=DEFAULT_CONTEXT_PRESSURE_STRONG_RATIO,
        help="fraction of semantic-compaction threshold where strong drop_context guidance begins",
    )
    parser.add_argument(
        "--context-pressure-urgent-ratio", type=float, default=DEFAULT_CONTEXT_PRESSURE_URGENT_RATIO,
        help="fraction of semantic-compaction threshold where urgent pre-compaction guidance begins",
    )
    parser.add_argument(
        "--context-maintenance-gate", choices=("off", "on"),
        default=DEFAULT_CONTEXT_MAINTENANCE_GATE,
        help=(
            "off leaves context cleanup advisory; on makes Core reject normal actions once "
            "working-context pressure reaches the maintenance threshold until cleanup releases it"
        ),
    )
    parser.add_argument(
        "--context-maintenance-budget", type=int,
        default=DEFAULT_CONTEXT_MAINTENANCE_BUDGET,
        help=(
            "cleanup-eligible raw working-context budget used by guidance/gate; "
            "defaults to half of the global semantic-compaction token limit"
        ),
    )
    parser.add_argument(
        "--context-maintenance-enter-ratio", type=float,
        default=DEFAULT_CONTEXT_MAINTENANCE_ENTER_RATIO,
        help="fraction of context-maintenance-budget where Core closes the maintenance gate",
    )
    parser.add_argument(
        "--context-maintenance-release-ratio", type=float,
        default=DEFAULT_CONTEXT_MAINTENANCE_RELEASE_RATIO,
        help="fraction of context-maintenance-budget cleanup must reach before Core reopens normal actions",
    )
    parser.add_argument(
        "--raw-reasoning-limit", "--worker-reasoning-attractor-tokens",
        dest="raw_reasoning_limit", type=int, default=DEFAULT_RAW_REASONING_LIMIT,
        help="deprecated compatibility no-op; reasoning follows the complete-step history window",
    )
    parser.add_argument(
        "--project-review-every", type=int, default=DEFAULT_PROJECT_REVIEW_EVERY,
        help="force a Project State review after this many executed shell operations; 0 disables periodic review",
    )
    parser.add_argument(
        "--state-cycle-after", type=int, default=DEFAULT_STATE_CYCLE_AFTER,
        help="optional legacy fallback: also cycle after N queued Worker iterations; 0 disables (default)",
    )
    parser.add_argument(
        "--state-keep", "--compact-keep",
        dest="state_keep", type=int, default=DEFAULT_STATE_KEEP,
        help="newest raw Worker messages kept verbatim when short-term context is trimmed (legacy alias: --compact-keep)",
    )
    parser.add_argument(
        "--no-state-management", "--no-compaction",
        dest="no_state_management", action="store_true",
        help="disable LLM state assimilation (legacy alias: --no-compaction)",
    )
    parser.add_argument("--max-reasoning-recoveries", type=int, default=DEFAULT_MAX_REASONING_RECOVERIES,
                        help="max consecutive recoveries from empty worker output with reasoning")
    parser.add_argument("--no-reasoning-recovery", action="store_true",
                        help="disable recovery from empty worker output/reasoning exhaustion")
    parser.add_argument("--command-timeout", type=int, default=300, help="hard upper bound per shell command")
    parser.add_argument("--output-limit", type=int, default=24000, help="hard Core admission limit for combined stdout/stderr chars; oversized output is withheld from Worker context")
    parser.add_argument("--max-tokens", type=int, default=8192)
    parser.add_argument(
        "--state-reasoning-budget",
        type=int,
        default=DEFAULT_STATE_REASONING_BUDGET,
        help="provider-level thinking budget for one State Manager assimilation attempt",
    )
    parser.add_argument(
        "--state-max-tokens",
        type=int,
        default=DEFAULT_STATE_MAX_TOKENS,
        help="total completion-token ceiling for one State Manager call, including reasoning",
    )
    parser.add_argument("--reasoning-effort", help="optional Worker reasoning_effort string passed unchanged to provider; omission uses provider/model default")
    parser.add_argument("--temperature", type=float, default=0.1)
    parser.add_argument("--interactive", action="store_true", help="terminal chat; Ctrl+Z requests a safe pause, /quit ends the session")
    parser.add_argument("--live", action="store_true", help="append-only human-friendly live terminal log with streamed Worker output")
    parser.add_argument("-v", "--verbose", action="store_true")
    obsolete = {
        "history_window", "worker_context_control", "context_pressure_guidance",
        "context_pressure_soft_ratio", "context_pressure_strong_ratio", "context_pressure_urgent_ratio",
        "context_maintenance_gate", "context_maintenance_budget", "context_maintenance_enter_ratio",
        "context_maintenance_release_ratio", "context_meter", "state_after", "state_keep",
        "state_cycle_after", "state_cycle_context_ratio", "state_cycle_token_limit",
        "no_state_management", "state_api_timeout", "state_reasoning_budget", "state_max_tokens",
    }
    for option in parser._actions:
        if option.dest in obsolete:
            option.help = "legacy compatibility no-op; active checkpoint/history architecture ignores this switch"
    return parser


def main() -> int:
    try:
        args = build_parser().parse_args()
        if args.self_test_bwrap:
            return run_bwrap_self_test(args.workdir)
        if (args.max_steps != -1 and args.max_steps <= 0) or args.command_timeout <= 0 or args.output_limit < 1000:
            raise AgentError("max-steps must be positive or -1; command-timeout must be positive and output-limit >= 1000")
        if args.api_timeout <= 0:
            raise AgentError("api-timeout must be positive")
        if args.max_tokens <= 0:
            raise AgentError("max-tokens must be positive")
        if args.project_review_every < 0:
            raise AgentError("project-review-every must be >= 0")
        if not 0 < args.history_context_high < 1:
            raise AgentError("history-context-high must be strictly between 0 and 1")
        if args.history_high < 1:
            raise AgentError("history-high must be >= 1")
        if args.worker_context_budget is not None and args.worker_context_budget <= 0:
            raise AgentError("worker-context-budget must be positive")
        if args.max_reasoning_loop_recoveries < 1:
            raise AgentError("max-reasoning-loop-recoveries must be positive")
        if args.max_reasoning_recoveries < 0:
            raise AgentError("max-reasoning-recoveries must be >= 0")
        return run_agent(args)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except AgentError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
