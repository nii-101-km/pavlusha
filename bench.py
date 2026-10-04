"""Dogfood benchmark for a deterministic context-maintenance gate.

Both arms receive the same model, task, sandbox, state manager, removable shell-result handles,
progressive ephemeral pressure guidance based only on cleanup-eligible raw observations, and a separate Core emergency-compaction threshold. The only intended
A/B difference is:

- pressure_guided: cleanup remains advisory; normal actions are always accepted until emergency compaction;
- maintenance_gate: at the configured entry pressure Core rejects shell/finish actions and accepts only
  context-replacement actions (drop_context tombstones or compact_context summaries) until cleanup lowers
  replaceable raw working-context pressure to the release boundary.

The gate message and inventories are reconstructed for the current Worker request only and are never
retained in raw history. The Worker still chooses which raw handles to replace and whether to preserve a
summary; Core only enforces the resource contract. Exact originals remain in controller evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

from pavlusha_agent.sandbox import run_shell

ROOT = Path(__file__).resolve().parent
DEFAULT_RUNS = ROOT / "bench-runs"
MODES = ("pressure_guided", "maintenance_gate")
BOUNDARY_TEST = (
    "tests.test_benchmark_contract.ContextPressureTokenLimitTests."
    "test_explicit_token_limit_overrides_ratio_and_triggers_at_boundary"
)
TIMEOUT_TEST = "tests.test_agent.ParseTests.test_timeout_is_clamped"
TARGETED_TESTS = (BOUNDARY_TEST, TIMEOUT_TEST)
DOGFOOD_TASK = (
    "Fix two small regressions in this project. "
    f"The tests {BOUNDARY_TEST} and {TIMEOUT_TEST} are failing. "
    "The explicit semantic-compaction token limit must trigger exactly at its boundary, and shell "
    "timeouts must never exceed the controller's max command timeout. Preserve the existing architecture, "
    "make the smallest correct implementation changes, do not weaken or edit tests, run both targeted "
    "tests, then run the full unit-test suite, and finish only with evidence that all pass."
)
EXPECTED_EVIDENCE_FILES = {
    "pavlusha_agent/state_cycle.py",
    "pavlusha_agent/sandbox.py",
    "tests/test_benchmark_contract.py",
    "tests/test_agent.py",
}


def _jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            out.append(value)
    return out


def _sha256_tree(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(
        p for p in root.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
    ):
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _copy_project(dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    for name in ("agent.py", "bench.py", "README.md", "example.env", "requirements.txt"):
        shutil.copy2(ROOT / name, dst / name)
    shutil.copytree(ROOT / "pavlusha_agent", dst / "pavlusha_agent", dirs_exist_ok=True)
    shutil.copytree(ROOT / "tests", dst / "tests", dirs_exist_ok=True)
    for path in list(dst.rglob("__pycache__")):
        shutil.rmtree(path, ignore_errors=True)
    for path in dst.rglob("*.pyc"):
        path.unlink(missing_ok=True)


def _inject_dogfood_bugs(workdir: Path) -> None:
    state_cycle = workdir / "pavlusha_agent" / "state_cycle.py"
    text = state_cycle.read_text(encoding="utf-8")
    good = "if turn.prompt_tokens >= threshold:"
    bad = "if turn.prompt_tokens > threshold:"
    if text.count(good) != 1:
        raise RuntimeError("expected exactly one context-boundary mutation site")
    state_cycle.write_text(text.replace(good, bad, 1), encoding="utf-8")

    sandbox = workdir / "pavlusha_agent" / "sandbox.py"
    text = sandbox.read_text(encoding="utf-8")
    good = "timeout = max(1, min(timeout, max_command_timeout))"
    bad = "timeout = max(1, min(timeout, max_command_timeout + 1))"
    if text.count(good) != 1:
        raise RuntimeError("expected exactly one timeout-clamp mutation site")
    sandbox.write_text(text.replace(good, bad, 1), encoding="utf-8")


def prepare_case(run_root: Path, mode: str, *, force: bool = False) -> tuple[Path, Path, Path]:
    if mode not in MODES:
        raise RuntimeError(f"unknown mode: {mode}")
    case = run_root / mode
    work = case / "work"
    state = case / "state"
    task_path = case / "task.txt"
    if case.exists():
        if not force:
            raise RuntimeError(f"case already exists: {case}; use --force or another --run-id")
        shutil.rmtree(case)
    case.mkdir(parents=True)
    _copy_project(work)
    _inject_dogfood_bugs(work)
    task_path.write_text(DOGFOOD_TASK + "\n", encoding="utf-8")
    context_control = "drop"
    guidance = "progressive"
    maintenance_gate = "on" if mode == "maintenance_gate" else "off"
    manifest = {
        "case": "maintenance_gate_context",
        "mode": mode,
        "context_meter": "hidden",
        "worker_context_control": context_control,
        "context_pressure_guidance": guidance,
        "context_maintenance_gate": maintenance_gate,
        "tests_sha256": _sha256_tree(work / "tests"),
        "task": DOGFOOD_TASK,
        "targeted_tests": list(TARGETED_TESTS),
        "expected_evidence_files": sorted(EXPECTED_EVIDENCE_FILES),
    }
    (case / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return work, state, task_path


def verify_case(case: Path) -> dict[str, Any]:
    work = case / "work"
    manifest = json.loads((case / "manifest.json").read_text(encoding="utf-8"))
    tests_unchanged = _sha256_tree(work / "tests") == manifest.get("tests_sha256")
    targeted_cmd = "python3 -m unittest -q " + " ".join(TARGETED_TESTS)
    targeted = run_shell(
        work,
        targeted_cmd,
        network=False,
        timeout=120,
        output_limit=12000,
    )
    full = run_shell(
        work,
        "python3 -m unittest -q",
        network=False,
        timeout=180,
        output_limit=16000,
    )
    result = {
        "tests_unchanged": tests_unchanged,
        "targeted_exit_code": targeted.exit_code,
        "targeted_timed_out": targeted.timed_out,
        "targeted_output": (targeted.stdout + "\n" + targeted.stderr)[-4000:],
        "full_exit_code": full.exit_code,
        "full_timed_out": full.timed_out,
        "full_output": (full.stdout + "\n" + full.stderr)[-4000:],
    }
    result["verified_pass"] = bool(
        tests_unchanged
        and targeted.exit_code == 0 and not targeted.timed_out
        and full.exit_code == 0 and not full.timed_out
    )
    (case / "verifier.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return result


def run_case(
    run_root: Path,
    mode: str,
    *,
    base_url: str,
    model: str | None,
    api_key: str,
    token_limit: int,
    managed_budget: int,
    context_budget: int | None,
    soft_ratio: float,
    strong_ratio: float,
    urgent_ratio: float,
    gate_enter_ratio: float,
    gate_release_ratio: float,
    max_steps: int,
    temperature: float,
    verbose: bool,
) -> int:
    work = run_root / mode / "work"
    state = run_root / mode / "state"
    task_path = run_root / mode / "task.txt"
    if not work.is_dir() or not task_path.is_file():
        raise RuntimeError(f"case is not prepared: {run_root / mode}")

    context_control = "drop"
    guidance = "progressive"
    maintenance_gate = "on" if mode == "maintenance_gate" else "off"
    cmd = [
        sys.executable,
        str(ROOT / "agent.py"),
        task_path.read_text(encoding="utf-8").strip(),
        "--workdir", str(work),
        "--state-dir", str(state),
        "--reset-state",
        "--context-meter", "hidden",
        "--worker-context-control", context_control,
        "--context-pressure-guidance", guidance,
        "--context-pressure-soft-ratio", str(soft_ratio),
        "--context-pressure-strong-ratio", str(strong_ratio),
        "--context-pressure-urgent-ratio", str(urgent_ratio),
        "--context-maintenance-gate", maintenance_gate,
        "--context-maintenance-budget", str(managed_budget),
        "--context-maintenance-enter-ratio", str(gate_enter_ratio),
        "--context-maintenance-release-ratio", str(gate_release_ratio),
        "--state-cycle-token-limit", str(token_limit),
        "--max-steps", str(max_steps),
        "--temperature", str(temperature),
        "--base-url", base_url,
    ]
    if model:
        cmd += ["--model", model]
    child_env = os.environ.copy()
    if api_key:
        child_env["AGENT_API_KEY"] = api_key
    if context_budget is not None:
        cmd += ["--worker-context-budget", str(context_budget)]
    if verbose:
        cmd.append("-v")

    print(f"\n=== {mode.upper()} ===", flush=True)
    print("$ " + shlex.join(cmd), flush=True)
    completed = subprocess.run(cmd, cwd=ROOT, env=child_env)
    try:
        verification = verify_case(run_root / mode)
        verdict = "PASS" if verification["verified_pass"] else "FAIL"
        print(
            f"[external-verifier] {mode}: {verdict}; "
            f"tests_unchanged={verification['tests_unchanged']}",
            flush=True,
        )
    except Exception as exc:
        verification = {"verified_pass": False, "error": f"{type(exc).__name__}: {exc}"}
        (run_root / mode / "verifier.json").write_text(
            json.dumps(verification, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"[external-verifier] {mode}: ERROR: {exc}", flush=True)
    return 0 if completed.returncode == 0 and verification.get("verified_pass") else 1


def _is_inspection_command(command: str) -> bool:
    c = " " + " ".join(command.split()) + " "
    needles = (
        " cat ", " sed ", " head ", " tail ", " grep ", " rg ",
        " find ", " ls ", " wc ", " awk ",
    )
    return any(n in c for n in needles)


def _known_project_files(work: Path) -> list[str]:
    files: list[str] = []
    for p in work.rglob("*"):
        if not p.is_file() or "__pycache__" in p.parts or p.suffix == ".pyc":
            continue
        files.append(str(p.relative_to(work)))
    return sorted(files, key=len, reverse=True)


def _referenced_project_files(command: str, known_files: list[str]) -> set[str]:
    normalized = command.replace("./", "")
    return {path for path in known_files if path in normalized}


def _is_broad_inspection(command: str, work: Path, known_files: list[str]) -> bool:
    c = " ".join(command.split())
    if "ls -R" in c or "find ." in c or "grep -R" in c or "grep -r" in c or "rg --files" in c:
        return True
    match = re.search(r"sed\s+-n\s+['\"]?(\d+)\s*,\s*(\d+)p['\"]?\s+(\S+)", c)
    if match and int(match.group(2)) - int(match.group(1)) + 1 >= 400:
        return True
    if re.search(r"(?:^|[;&|]\s*)cat\s+", c):
        for rel in _referenced_project_files(c, known_files):
            try:
                if (work / rel).stat().st_size >= 12000:
                    return True
            except OSError:
                pass
    return False


def score_case(case: Path) -> dict[str, Any]:
    state_dir = case / "state"
    work = case / "work"
    state_log = _jsonl(state_dir / "state.log")
    experiment = _jsonl(state_dir / "experiment.jsonl")
    turns = [e for e in experiment if e.get("kind") == "worker_turn"]
    actions = [e for e in experiment if e.get("kind") == "worker_action"]
    # Older runs recorded destructive context_drop_applied events. Current runs record in-place
    # context_replacement_applied events for both drop_context -> TOMBSTONE and
    # compact_context -> COMPACTED. Score both so existing benchmark archives remain comparable.
    legacy_drops = [e for e in experiment if e.get("kind") == "context_drop_applied"]
    replacements = [e for e in experiment if e.get("kind") == "context_replacement_applied"]
    tombstone_replacements = [e for e in replacements if e.get("mode") == "tombstone"]
    compact_replacements = [e for e in replacements if e.get("mode") == "compacted"]
    notices = [e for e in experiment if e.get("kind") == "context_notice"]
    gate_events = [e for e in experiment if e.get("kind") == "maintenance_gate"]
    operation_events = [e for e in state_log if e.get("kind") == "operation_recorded"]
    operations = [e.get("operation", {}) for e in operation_events if isinstance(e.get("operation"), dict)]
    op_telemetry = {
        str(e.get("op_id")): e for e in experiment
        if e.get("kind") == "operation_telemetry" and e.get("op_id")
    }
    commands = [str(op.get("command", "")) for op in operations]
    normalized = [" ".join(c.split()) for c in commands if c.strip()]
    counts = Counter(normalized)
    repeated_exact = sum(max(0, n - 1) for n in counts.values())
    known_files = _known_project_files(work)
    inspection_ops = [op for op in operations if _is_inspection_command(str(op.get("command", "")))]
    broad_ops = [
        op for op in inspection_ops
        if _is_broad_inspection(str(op.get("command", "")), work, known_files)
    ]
    inspected_files: set[str] = set()
    for op in inspection_ops:
        inspected_files |= _referenced_project_files(str(op.get("command", "")), known_files)
    outside_expected = sorted(inspected_files - EXPECTED_EVIDENCE_FILES)
    prompt = [e.get("prompt_tokens") for e in turns if isinstance(e.get("prompt_tokens"), int)]
    completion = [e.get("completion_tokens") for e in turns if isinstance(e.get("completion_tokens"), int)]
    reasoning = [e.get("reasoning_tokens") for e in turns if isinstance(e.get("reasoning_tokens"), int)]
    removable = [
        e.get("working_removable_approx_tokens")
        for e in turns if isinstance(e.get("working_removable_approx_tokens"), int)
    ]
    total_working = [
        e.get("working_total_approx_tokens")
        for e in turns if isinstance(e.get("working_total_approx_tokens"), int)
    ]
    protected_working = [
        e.get("working_protected_approx_tokens")
        for e in turns if isinstance(e.get("working_protected_approx_tokens"), int)
    ]
    test_commands = [c for c in commands if "unittest" in c or "pytest" in c]
    drop_actions = [e for e in actions if e.get("action") == "drop_context"]
    compact_actions = [e for e in actions if e.get("action") == "compact_context"]
    drop_reasons = [
        str(item.get("reason", ""))
        for event in drop_actions
        for item in event.get("items", [])
        if isinstance(item, dict) and str(item.get("reason", ""))
    ]
    compact_summaries = [
        str(item.get("summary", ""))
        for event in compact_actions
        for item in event.get("items", [])
        if isinstance(item, dict) and str(item.get("summary", ""))
    ]
    legacy_drop_tokens = sum(int(e.get("approx_tokens_removed", 0) or 0) for e in legacy_drops)
    tombstone_freed_tokens = sum(
        int(e.get("approx_tokens_freed", 0) or 0) for e in tombstone_replacements
    )

    verifier: dict[str, Any] = {}
    if (case / "verifier.json").exists():
        try:
            verifier = json.loads((case / "verifier.json").read_text(encoding="utf-8"))
        except Exception:
            verifier = {}
    state: dict[str, Any] = {}
    if (state_dir / "state.json").exists():
        try:
            state = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
        except Exception:
            state = {}

    return {
        "mode": case.name,
        "finished": state.get("run", {}).get("status") == "finished",
        "verified_pass": bool(verifier.get("verified_pass", False)),
        "tests_unchanged": bool(verifier.get("tests_unchanged", False)),
        "worker_turns": len(turns),
        "shell_actions": sum(1 for e in actions if e.get("action") == "shell"),
        "context_drop_actions": len(drop_actions),
        "context_compact_actions": len(compact_actions),
        "context_replacements_applied": len(replacements),
        "tombstone_replacements_applied": len(tombstone_replacements),
        "compact_replacements_applied": len(compact_replacements),
        "replaced_context_items": sum(len(e.get("ids", [])) for e in replacements),
        "replacement_tokens_before": sum(int(e.get("approx_tokens_before", 0) or 0) for e in replacements),
        "replacement_tokens_after": sum(int(e.get("approx_tokens_after", 0) or 0) for e in replacements),
        "replacement_tokens_freed": sum(int(e.get("approx_tokens_freed", 0) or 0) for e in replacements),
        # Legacy compatibility fields. A current drop_context tombstones material, so these count
        # tombstone replacements and report net prompt savings rather than pretending the item is
        # physically absent.
        "context_drop_applied": len(legacy_drops) + len(tombstone_replacements),
        "dropped_context_items": (
            sum(len(e.get("ids", [])) for e in legacy_drops)
            + sum(len(e.get("ids", [])) for e in tombstone_replacements)
        ),
        "pressure_notice_soft": sum(1 for e in notices if e.get("level") == "soft"),
        "pressure_notice_strong": sum(1 for e in notices if e.get("level") == "strong"),
        "pressure_notice_urgent": sum(1 for e in notices if e.get("level") == "urgent"),
        "maintenance_gate_enters": sum(1 for e in gate_events if e.get("event") == "enter"),
        "maintenance_gate_rejects": sum(1 for e in gate_events if e.get("event") == "reject"),
        "maintenance_gate_releases": sum(1 for e in gate_events if e.get("event") == "release"),
        "maintenance_gate_emergencies": sum(1 for e in gate_events if e.get("event") == "emergency"),
        "dropped_approx_tokens": legacy_drop_tokens + tombstone_freed_tokens,
        "drop_intents": [str(e.get("intent", "")) for e in legacy_drops],
        "drop_reasons": drop_reasons,
        "compact_summaries": compact_summaries,
        "compaction_cycles": sum(1 for e in state_log if e.get("kind") == "state_cycle_committed"),
        "context_triggers": sum(1 for e in state_log if e.get("kind") == "state_cycle_trigger"),
        "prompt_tokens_total": sum(prompt),
        "prompt_tokens_max": max(prompt) if prompt else None,
        "completion_tokens_total": sum(completion),
        "reasoning_tokens_total": sum(reasoning),
        "working_removable_tokens_max": max(removable) if removable else 0,
        "working_total_tokens_max": max(total_working) if total_working else 0,
        "working_protected_tokens_max": max(protected_working) if protected_working else 0,
        "inspection_commands": len(inspection_ops),
        "broad_inspection_commands": len(broad_ops),
        "repeated_exact_commands": repeated_exact,
        "inspection_output_chars": sum(
            int(op_telemetry.get(str(op.get("id")), {}).get("stdout_chars", 0) or 0)
            + int(op_telemetry.get(str(op.get("id")), {}).get("stderr_chars", 0) or 0)
            for op in inspection_ops
        ),
        "unique_inspected_files": len(inspected_files),
        "outside_expected_files": len(outside_expected),
        "outside_expected_file_names": outside_expected,
        "test_commands": len(test_commands),
        "commands": commands,
    }


def print_comparison(run_root: Path) -> None:
    rows = []
    for mode in MODES:
        case = run_root / mode
        if case.exists():
            rows.append(score_case(case))
    if not rows:
        raise RuntimeError(f"no cases found under {run_root}")

    columns = [
        "mode", "finished", "verified_pass", "worker_turns", "shell_actions",
        "context_drop_actions", "context_compact_actions", "context_replacements_applied",
        "tombstone_replacements_applied", "compact_replacements_applied",
        "replaced_context_items", "replacement_tokens_freed",
        "pressure_notice_soft", "pressure_notice_strong", "pressure_notice_urgent",
        "maintenance_gate_enters", "maintenance_gate_rejects", "maintenance_gate_releases",
        "maintenance_gate_emergencies", "compaction_cycles", "prompt_tokens_total", "prompt_tokens_max",
        "working_removable_tokens_max", "working_total_tokens_max", "working_protected_tokens_max",
        "inspection_commands", "broad_inspection_commands",
        "repeated_exact_commands", "inspection_output_chars", "unique_inspected_files",
        "outside_expected_files", "test_commands",
    ]
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in columns}
    print("  ".join(c.ljust(widths[c]) for c in columns))
    print("  ".join("-" * widths[c] for c in columns))
    for row in rows:
        print("  ".join(str(row.get(c, "")).ljust(widths[c]) for c in columns))

    out = run_root / "comparison.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\nDetailed comparison: {out}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Qwen progressive-guidance vs hard maintenance-gate dogfood bench")
    sub = p.add_subparsers(dest="command", required=True)

    prep = sub.add_parser("prepare", help="prepare identical pressure_guided/maintenance_gate dogfood workspaces")
    prep.add_argument("--run-id", default=time.strftime("%Y%m%d-%H%M%S"))
    prep.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS)
    prep.add_argument("--force", action="store_true")

    pair = sub.add_parser("pair", help="prepare and run pressure_guided/maintenance_gate against the same two bugs")
    pair.add_argument("--run-id", default=time.strftime("%Y%m%d-%H%M%S"))
    pair.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS)
    pair.add_argument("--force", action="store_true")
    pair.add_argument("--base-url", default="http://127.0.0.1:1234/v1")
    pair.add_argument("--model")
    pair.add_argument("--api-key", default="")
    pair.add_argument(
        "--token-limit", type=int, default=12000,
        help="global provider prompt-token threshold for Core semantic compaction in both arms",
    )
    pair.add_argument(
        "--managed-budget", type=int, default=6000,
        help="cleanup-eligible removable working-context budget used by pressure guidance and the gate",
    )
    pair.add_argument("--soft-ratio", type=float, default=0.50)
    pair.add_argument("--strong-ratio", type=float, default=0.75)
    pair.add_argument("--urgent-ratio", type=float, default=0.90)
    pair.add_argument("--gate-enter-ratio", type=float, default=0.75)
    pair.add_argument("--gate-release-ratio", type=float, default=0.55)
    pair.add_argument("--context-budget", type=int)
    pair.add_argument("--max-steps", type=int, default=36)
    pair.add_argument("--temperature", type=float, default=0.0)
    pair.add_argument("--order", choices=("guided-first", "gate-first"), default="guided-first")
    pair.add_argument("-v", "--verbose", action="store_true")

    score = sub.add_parser("score", help="score an existing pair")
    score.add_argument("run_root", type=Path)
    return p


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "prepare":
        run_root = args.runs_dir.expanduser().resolve() / args.run_id
        for mode in MODES:
            prepare_case(run_root, mode, force=args.force)
        print(run_root)
        return 0

    if args.command == "pair":
        if args.token_limit <= 0 or args.managed_budget <= 0 or args.max_steps <= 0:
            raise SystemExit("token-limit, managed-budget, and max-steps must be positive")
        if not (0.0 < args.soft_ratio < args.strong_ratio < args.urgent_ratio < 1.0):
            raise SystemExit("ratios must satisfy 0 < soft < strong < urgent < 1")
        if not (0.0 < args.gate_release_ratio < args.gate_enter_ratio < 1.0):
            raise SystemExit("gate ratios must satisfy 0 < release < enter < 1")
        if args.context_budget is not None and args.token_limit >= args.context_budget:
            raise SystemExit("token-limit must be smaller than context-budget")
        run_root = args.runs_dir.expanduser().resolve() / args.run_id
        for mode in MODES:
            prepare_case(run_root, mode, force=args.force)
        order = MODES if args.order == "guided-first" else tuple(reversed(MODES))
        return_codes = []
        for mode in order:
            return_codes.append(run_case(
                run_root, mode,
                base_url=args.base_url,
                model=args.model,
                api_key=args.api_key,
                token_limit=args.token_limit,
                managed_budget=args.managed_budget,
                context_budget=args.context_budget,
                soft_ratio=args.soft_ratio,
                strong_ratio=args.strong_ratio,
                urgent_ratio=args.urgent_ratio,
                gate_enter_ratio=args.gate_enter_ratio,
                gate_release_ratio=args.gate_release_ratio,
                max_steps=args.max_steps,
                temperature=args.temperature,
                verbose=args.verbose,
            ))
        print("\n=== COMPARISON ===")
        print_comparison(run_root)
        return 0 if all(code == 0 for code in return_codes) else 1

    if args.command == "score":
        print_comparison(args.run_root.expanduser().resolve())
        return 0

    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
