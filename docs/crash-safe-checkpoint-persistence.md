# Crash-safe checkpoint persistence audit — 2026-10-13

The existing publisher already serialized Project State, handoff and recovery metadata in
one complete `state.json` image. It validated the checkpoint before publication, flushed and
fsynced a same-directory temporary file, atomically replaced the primary with `os.replace`,
then fsynced the directory. A process interruption selected either the old or new complete
generation. Publication was not rewritten.

The missing behavior was fallback after a missing, incomplete or corrupt primary: the old
implementation rejected recovery even when an earlier generation could have been preserved.

## Changes

`state_store.py` now preserves the previous validated controller image before replacing its
checkpoint generation. Historical filenames include generation and checkpoint digest; existing
historical images are never overwritten or deleted. Archive publication uses its own flushed,
fsynced temporary file and atomic replacement, completed before primary publication. An archive
failure aborts the new checkpoint while leaving the primary intact.

Startup prefers a valid primary. Otherwise it validates published historical images with the
existing validator, chooses the newest valid generation, restores its paired State/handoff and
metadata, and atomically repairs the primary. All temporary files are ignored. No valid image
means the existing explicit recovery error. Existing single-file checkpoints remain readable.

Explicit reset preserves the historical directory under a unique `checkpoint-history.reset-*`
name, preventing accidental recovery of the previous run while retaining its files.

The 10-operation publication interval, 200-model-step/85%-context reset policy, checkpoint format,
validator, provider and Worker logic are unchanged by this task.

Files changed for this task: `pavlusha_agent/state_store.py`,
`tests/test_atomic_checkpoint_recovery.py`, new `tests/test_checkpoint_history.py`,
`README.md`, `docs/atomic-checkpoint-recovery.md`, and this report. Three existing scripted
tests (`test_functions.py`, `test_operation_loop.py`, `test_context_preflight.py`) were aligned
with the already implemented periodic checkpoint protocol; no scheduling change was made.

## Verification

Full suite with existing project dependencies: **400 tests, OK, 60 skipped**. Tests requiring
a local HTTP listener and private Xvfb display ran outside the restrictive execution sandbox.
Dependencies were installed only in `/tmp/pavlusha-test-deps`, not added to the project.

The atomic persistence and history suites cover candidate preparation failure, partial State
and handoff writes, complete temporary files before publication, injected replacement errors,
failure immediately after replacement, directory fsync failure, archive write/flush/publication
failure, interrupted repair, invalid historical generations, wrong tasks, reset isolation and
legacy valid files. Real subprocess tests terminate abruptly at publication boundaries and
restore in a fresh interpreter. A missing primary is also recovered in a fresh interpreter.

## Limits

Process-crash consistency is tested. File and directory fsync request Linux crash durability,
but no physical power-loss or faulty-storage test was performed; universal power-loss durability
is not claimed. Ancestor creation, audit logs and project work files are outside this transaction.
Only one controller writer per state directory is supported. Fallback restores the historical
image's operation ledger; newer records from a corrupt primary are not fabricated or combined.
Archive size grows because no retention policy was introduced. A corrupt primary with no valid
historical image still fails explicitly.
