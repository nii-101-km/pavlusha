"""Command-line interface and bubblewrap self-test."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from .config import DEFAULT_MAX_REASONING_RECOVERIES, DEFAULT_PROJECT_MAP, DEFAULT_PROJECT_REVIEW_EVERY
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
    result = run_shell(workdir, probe, network=False, timeout=20)
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
        description="Autonomous LLM worker in bubblewrap with controller-owned persistent state.",
        allow_abbrev=False,
    )
    parser.add_argument("task", nargs="*", help="task text; if omitted, read it from stdin")
    parser.add_argument("--workdir", default="./agent-work", help="persistent writable working directory")
    parser.add_argument("--network", action=argparse.BooleanOptionalAction, default=True,
                        help="allow requested networked shell/GUI/Expert operations (default: on; --no-network disables)")
    parser.add_argument("--functions", action="append", metavar="MODULE.py", help="load trusted local synchronous Python functions for this run; repeat for multiple modules (not sandboxed)")
    parser.add_argument(
        "--gui", action=argparse.BooleanOptionalAction, default=True,
        help="private 800x600 Xvfb GUI actions (default: enabled when dependencies are available; --no-gui disables)",
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
    parser.add_argument("--reasoning-loop-recovery", choices=("off", "observe", "recover"),
                        default="recover", help="streamed lexical loop detection (default: recover); off disables detection")
    parser.add_argument("--reasoning-loop-diagnostics", action="store_true",
                        help="show detailed reasoning detector windows and similarity (default: off)")
    parser.add_argument("--max-reasoning-loop-recoveries", type=int, default=3,
                        help="maximum consecutive retry generations after a lexical loop (default: 3)")
    parser.add_argument("--max-steps", type=int, default=-1, help="Worker step limit (default: -1, unlimited)")
    parser.add_argument(
        "--state-dir",
        help="controller-owned persistent state directory; default: <workdir>.pavlusha-state",
    )
    parser.add_argument("--reset-state", action="store_true", help="discard existing persistent state for this state-dir")
    parser.add_argument(
        "--worker-context-budget",
        dest="worker_context_budget", type=int, default=None,
        help="override Worker context budget; by default discover active LM Studio context_length",
    )
    parser.add_argument(
        "--project-map", choices=("off", "on"), default=DEFAULT_PROJECT_MAP,
        help=(
            "on (default) supplies a frozen checkpoint snapshot of the Python project map; "
            "file SHA-256 values are cached outside /work and only changed files are reparsed; "
            "missing indexer dependencies disable the map with a diagnostic"
        ),
    )
    parser.add_argument("--history-context-high", type=float, default=0.85,
                        help="HIGH fraction of provider-reported prompt usage / model capacity (default: 0.85)")
    parser.add_argument("--history-high", type=int, default=200,
                        help="require a fresh checkpoint and full history reset at this many retained Worker steps (default: 200)")
    parser.add_argument(
        "--project-review-every", type=int, default=DEFAULT_PROJECT_REVIEW_EVERY,
        help="force a Project State review after this many executed shell/function operations; 0 disables periodic review",
    )
    parser.add_argument("--max-reasoning-recoveries", type=int, default=DEFAULT_MAX_REASONING_RECOVERIES,
                        help="max consecutive recoveries from empty worker output with reasoning")
    parser.add_argument("--no-reasoning-recovery", action="store_true",
                        help="disable recovery from empty worker output/reasoning exhaustion")
    parser.add_argument("--command-timeout", type=int, default=300, help="hard upper bound per shell command")
    parser.add_argument("--output-limit", type=int, default=24000, help="hard Core admission limit for combined stdout/stderr chars; oversized output is withheld from Worker context")
    parser.add_argument("--max-tokens", type=int, default=8192)
    parser.add_argument("--reasoning-effort", help="optional Worker reasoning_effort string passed unchanged to provider; omission uses provider/model default")
    parser.add_argument("--temperature", type=float, default=0.1)
    parser.add_argument("--interactive", action=argparse.BooleanOptionalAction, default=True,
                        help="terminal chat (default: on; --no-interactive for pipes/batch); Ctrl+Z pauses, /quit ends")
    parser.add_argument("--live", action=argparse.BooleanOptionalAction, default=True,
                        help="append-only live Worker log (default: on; --no-live disables progress, chat remains available)")
    parser.add_argument("-v", "--verbose", action="store_true")
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
