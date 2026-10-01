# Project State v0 — recovery checkpoint

Project State is the single durable semantic recovery document used by the active Worker runtime.
It is **not** a transcript, a reasoning summary, a file inventory, or a second source of truth.

## Goal

Minimize recovery tax after bounded working memory is discarded. After a context reset the Worker
should recover from:

1. immutable TASK;
2. checkpoint PROJECT MAP snapshot;
3. Core-owned PROJECT STATE snapshot;
4. current files, tests and environment.

The runtime explicitly tells the Worker that context loss is normal. Missing memory is never evidence
that work was not performed.

## Shape

```text
PROJECT STATE
  revision
  last_review_operation

  DESIGN
    Dxxx ACTIVE|SUPERSEDED
      decision
      rationale
      deviation? -> Vxxx

  WORK
    Wxxx PLANNED|ACTIVE|DONE|BLOCKED|SUPERSEDED
      objective
      deliverables[]
      evidence[]
      reason?      # BLOCKED
      deviation?   # SUPERSEDED

  DEVIATIONS
    Vxxx ACTIVE
      affects[]
      original
      actual
      reason

  RECOVERY
    active -> Wxxx | null
```

DESIGN stores durable architectural decisions, not the current file tree. Planned files belong under
WORK.deliverables. Current physical structure belongs to Project Map/current files.

## Evidence

Evidence is selected by the Worker for future recovery. It must point to concrete material from the
project world:

- **FILES**;
- and/or **SHELL OPERATIONS / RESULTS**.

Evidence is not an explanation, conclusion, confidence statement, or restatement of the WORK objective.

Core deliberately does **not** decide whether evidence is sufficient, relevant, true enough, or the right
kind for a particular task. It only keeps the evidence field structurally well-formed. This avoids teaching
Core every possible kind of work and avoids exact command/ledger matching bureaucracy.

## Core-owned mutations

The Worker cannot edit Project State through shell. It uses typed actions:

- `project_init`
- `project_update`
- `project_review_complete`

Core owns IDs, transition validation, persistence and atomic publication.
Initial Project State is a mandatory execution plan, not a permission slip: before shell work Core requires
at least two WORK items, exactly one ACTIVE and at least one PLANNED. The prompt asks the Worker to
materialize all currently identifiable objectives and to keep final verification separate when applicable.
A project with unfinished work must have exactly one ACTIVE work item. BLOCKED requires a reason;
SUPERSEDED requires a valid deviation. DONE does not trigger semantic evidence validation.

Rejected Project State mutations are returned to the Worker as recoverable controller feedback rather than
terminating the run. Evidence itself is only structurally normalized by Core.

## Periodic Project State review and history checkpoints

`--project-review-every N` opens a small attention questionnaire after N executed shell operations (default 10).
It is not a history checkpoint and never refreshes frozen Map/State snapshots or resets Recent History.
For that one review turn normal shell/finish actions are unavailable. The Worker either records durable
DESIGN/WORK/DEVIATION changes with `project_update`, or answers `project_review_skip` when there is nothing
new worth preserving. Either response acknowledges the questionnaire and normal work resumes.

History capacity is separate. At `--history-high` or the pre-request `--history-context-high` threshold, Core requires the same real history checkpoint. Normal shell/finish
actions are blocked while it is required. The Worker may materialize durable changes with `project_update`, then
calls `project_review_complete`. Only an accepted required history checkpoint refreshes/freezes Project Map and
Project State, emits `PREFIX CHANGED · checkpoint snapshot refresh` in Live, archives the old chronological
Recent History, and resets the delta. `project_review_complete` is rejected when no history checkpoint is required.

Between history checkpoints the prompt is SYSTEM → TASK → frozen MAP → frozen STATE → optional latest HANDOFF → chronological
RECENT HISTORY. A periodic questionnaire is appended as a temporary suffix instruction so it does not rewrite
the frozen early prefix. Local State mutations update the canonical document and are acknowledged in history;
they do not rewrite the frozen State message until a real history checkpoint.

Each step retains reasoning → action → result together. At HIGH retained steps, the existing review
gate requires a fresh recovery checkpoint and blocks ordinary shell/finish actions. Keep the entire
history during review and retries. Only after review succeeds and fresh Map/State snapshots exist,
archive and clear history completely. Resume from the new baseline and an empty delta. There is no
partial LOW/HIGH cut in active execution. `--history-low` is not supported. The count threshold
remains independent of the context-fraction trigger; the full history must still fit the checkpoint
request. Context pressure uses provider-reported prompt-token usage, not a prediction of the next request's size.
Provider/validation failures never authorize dropping history. Nothing is summarized or automatically
restored. Reasoning remains working hypotheses, not durable truth.

`--raw-reasoning-limit` and its legacy alias are deprecated no-ops. There is no separate reasoning
retention, overflow checkpoint, reset notice, or active reasoning archive. Model generation limits
and output admission are unchanged.

## Recovery benchmark

After a successful overflow checkpoint/reset, measure recovery steps/tool calls/tokens until the first action
that advances unfinished work rather than reconstructing the past. Recovery uses TASK + the
checkpoint baseline + retained delta + current files/tests/environment and Worker-selected evidence.

### Transient checkpoint handoff

`project_review_complete` accepts one optional `handoff` string, at most 2048 UTF-8 bytes.
The Worker prompt requests immediate focus/next action/unfinished verification, not a State or
history summary. Core commits it together with Project State and checkpoint metadata in
`state.json.recovery_checkpoint`; top-level `checkpoint_handoff` remains the live prompt copy. Omission replaces it with empty
text for compatibility. Only the latest value is loaded at startup and supplied after State/Map
and before Recent History. Periodic reviews neither create nor replace it. Existing exact-action
audit archives are unchanged. The handoff may be stale; current files, State and Map take precedence.

### Atomic generation and cold restart

`project_init` atomically commits the initial recovery generation. Only successful true HIGH
`project_review_complete` replaces it with the next reviewed Project State, matching handoff and
metadata. Accepted ordinary/periodic updates remain the live working document until the next HIGH;
they do not replace the cold-restart recovery point. The 2048-byte bound and evidence authority are
unchanged. No semantic evidence checks are added.

Cold startup with an existing state directory loads a structurally valid, integrity-checked
`recovery_checkpoint` envelope, then restores its Project State and handoff. Missing/invalid
committed State is **RECOVERY FAILED**. Temporary candidates, logs, Map, handoff alone and `/work`
cannot authorize recovery. Older files lacking the envelope are explicitly rejected, not inferred
or automatically migrated. A nonexistent directory or intentional `--reset-state` uses the normal
new-project initialization path.

Post-checkpoint `/work` edits remain intact. Project Map is deterministically refreshed from those
current files after successful State recovery. The operation ledger/counter is retained to avoid
reusing OP IDs; the committed State's `last_review_operation` is restored unchanged. Core does not
infer new State from the world. The first resumed prompt contains the matching handoff once as
checkpoint context, outside Recent History and reasoning-loop markers.

Publication uses one complete temp file, file fsync, same-directory atomic replace and directory
fsync. Failed pre-publication writes leave the old generation authoritative. A failure after replace
may already have published the new complete generation. There is no fallback from corrupt committed
data. See `docs/atomic-checkpoint-recovery.md` for exact guarantees and failure-injection results.
