# Pavlusha 0.6.0

## Changes

- Periodic checkpoints atomically commit current Project State and a nonempty operational handoff; History and the frozen prompt prefix remain intact.
- Missing or invalid primary State can recover from the newest validated published checkpoint-history generation.
- Terminal bracketed paste preserves multiline messages until a separate Enter submission and filters pasted control bytes.
- Worker sandbox tools provide bounded numbered reads, definition search, exact replacements and explicit check results with full logs. Bash pipelines use pipefail.
- GUI drag observations use hollow START/END crosshairs instead of an arrow that can obscure application wires.
- Worker reasoning defaults to low; token-ceiling feedback distinguishes missing actions from oversized actions.
- Worker instructions distinguish source inspection, behavioral tests and browser verification.

## Limits

Checkpoint history currently grows without an automatic size limit or retention deletion.
Fallback restores the selected historical image's ledger, without reconstructing newer operations.
One controller writer per State directory is supported. Physical power-loss durability is not tested.
Verification guidance and run-check do not independently judge the sufficiency of Worker evidence.

## Reproducible verification

Tested on Linux x86_64 with CPython 3.12.3. A fresh venv with
include-system-site-packages=false was installed from requirements-test.txt; all
resolved runtime and test dependencies are pinned in requirements-test-lock.txt.
jsonschema comes from the venv, user-site packages are disabled, and no PYTHONPATH
fallback is used. pip check passes. See the README Tests section for the clean-venv recipe.

Full discovery passed in both a fresh requirements-test.txt environment and a second
fresh environment installed from requirements-test-lock.txt: 410 tests, OK;
350 passed and 60 skipped. The skipped tests comprise 33 optional DOCX tests,
18 optional XLSX tests and 9 opt-in browser, bubblewrap and network tests.
Both environments pass pip check; the second environment's pip freeze exactly
matches the lock file. Private X11 tests additionally require host Xvfb/setxkbmap tools;
Python dependency pinning does not install or pin host system packages. Live model
inference and physical power-loss tests were not performed.

## Proposed commit contents

The following paths are the release candidates; this list does not stage or commit them.
The three internal documents below remain local and are excluded:

- docs/planned-fixes.md
- docs/subsystem-dogfood-audit-20261013.md
- docs/sync-fix-dogfood-audit-20261013.md

```text
PROJECT_STATE.md
README.md
docs/atomic-checkpoint-recovery.md
docs/crash-safe-checkpoint-persistence.md
docs/gui-tools.md
docs/interactive-chat.md
docs/release-0.6.0.md
docs/worker-action-contract.md
pavlusha_agent/__init__.py
pavlusha_agent/cli.py
pavlusha_agent/config.py
pavlusha_agent/gui.py
pavlusha_agent/interactive.py
pavlusha_agent/runtime.py
pavlusha_agent/sandbox.py
pavlusha_agent/state_store.py
pavlusha_agent/worker_contract.py
pavlusha_agent/workspace_tools.py
requirements-test-lock.txt
tests/test_atomic_checkpoint_recovery.py
tests/test_checkpoint_history.py
tests/test_checkpoint_snapshots.py
tests/test_context_preflight.py
tests/test_functions.py
tests/test_gui_tools.py
tests/test_interactive.py
tests/test_interactive_paste.py
tests/test_operation_loop.py
tests/test_reasoning_effort.py
tests/test_reasoning_loop.py
tests/test_reasoning_window.py
tests/test_runtime_lifecycle.py
tests/test_worker_structured_output.py
tests/test_workspace_tools.py
```
