# Atomic checkpoint handoff and strict cold restart

## Previous behavior and problem

Before changes, inspected StateStore serialization/atomic writes, Project State validators,
HIGH publication order, startup/Map refresh, process restart tests, and both recent reports:
`runtime-lifecycle-fixes.md` and `reasoning-loop-recovery.md`. No applicable AGENTS.md was found.
Baseline: 146 existing tests passed in the repository virtual environment, with three restricted-
environment skips (local HTTP permission and two opt-in live network tests).

`state.json` used controller schema 2. `_write_atomic` serialized a complete image into a same-
directory `.state.json.tmp.<PID>`, flushed/fsynced the file, then called `os.replace`. It did not
sync the containing directory. State and top-level `checkpoint_handoff` were already atomically
written together at accepted HIGH completion, but ordinary `project_update` writes replaced the
working State before review completion. A crash during review could therefore load a newer,
unreviewed State with the old handoff; no distinct committed checkpoint snapshot remained.

Startup loaded that latest working file, tolerated missing Project State by adding an empty one,
and created a new file if `state.json` was missing. This could bootstrap a project from a state
directory containing only crash debris instead of failing recovery. Corrupt JSON failed, but
there was no complete structural validation of persisted Project State at the recovery boundary.

HIGH order was: accepted State/handoff write, audit log append, Map refresh, frozen State/Map
prompt refresh, history archive/fsync, history reset. Periodic review persisted a State update
or acknowledgement while retaining prefix/history. Map startup always refreshed against `/work`.
Previous abrupt-exit tests used a separate process with scripted provider/shell filesystem writes.
These architectural responsibilities remain separate.

## Final on-disk layout

The runtime keeps **one authoritative file**, `state.json`, with controller schema 3 and active
working fields. Schemas 1 and 2 are rejected without migration. A nested checkpoint envelope
is the committed recovery point:

```text
state.json                         controller schema_version = 3
  task                             original text / SHA-256 / immutable
  version                          current controller write version
  project_state                    live working State; may be newer than checkpoint
  operations / counters            factual ledger, including post-checkpoint operations
  run / metadata                   current process/controller metadata
  recovery_checkpoint              the single committed recovery generation
    schema_version                 1
    generation                     positive monotonically advanced ID
    kind                           initial | high
    project_state                  complete committed Project State
    handoff                        one matching UTF-8 text field
    state_version                  controller version that committed this generation
    operation_count                checkpoint operation boundary
    step                           runtime step at publication
    task_sha256                    matching immutable task
    committed_at                   timezone-aware ISO timestamp
    sha256                         digest of every preceding envelope field
.state.json.tmp.<PID>               private complete or partial candidate; never a recovery source
state.log / experiment.jsonl        diagnostic/audit data; never recovery authority
project_map.json                   deterministic world-derived cache; outside generation
history_archive.jsonl              exact archived history; never auto-restored
```

There is only one envelope. A new HIGH replaces it, rather than accumulating generations or
handoffs. Working State and the envelope are separate views in the same atomic file image;
this preserves the last committed generation even when review updates the live working view.
The handoff exists only inside the committed envelope; startup and cold restart use that value.
The digest binds State, handoff and metadata together; it
is an integrity check, not authentication or semantic verification.

## Publication protocol

1. Build candidate reviewed Project State privately, using existing mutation/review invariants.
2. Validate handoff type, UTF-8 encoding and 2048-byte limit.
3. Build a complete envelope with generation ID, State, handoff, task and runtime metadata.
4. Validate the candidate structurally and compute/check the digest. Until this succeeds, no
   published bytes change. Initialization creates generation 1 (`initial`) with empty handoff;
   each HIGH increments the prior generation (`high`).
5. Serialize the complete containing controller image to `.state.json.tmp.<PID>` in the same
   directory. Flush and fsync the candidate file.
6. Atomically `os.replace(candidate, state.json)`. This is the logical publication boundary.
7. Open/fsync the containing state directory and close its descriptor.
8. Continue the existing audit/Map/frozen-prefix/history archival/reset path.

All State/handoff/metadata changes are in one replacement. A failure while building or writing a
candidate, or before replacement, leaves the prior envelope in the published file. A process death
immediately after replacement exposes the complete new envelope. If directory fsync or a later
audit/Map/archive step fails, replacement may already have succeeded; the code does not claim
rollback. Cold restart selects the published file's envelope, never a partially constructed temp.

Ordinary updates, periodic acknowledgements, shell ledger writes and finish preserve the current
envelope. They do not publish a new recovery generation. This deliberately means a cold restart
loads the last initialized/HIGH checkpoint, rather than later working mutations (including any
post-checkpoint DONE update). A successful finish is not automatically converted into HIGH.

## Structural validation

Recovery validates the existing committed document without repairing it:

- Controller schema, task text/hash/immutability, write version, required runtime metadata and
  factual ledger/counter consistency.
- Exact envelope fields and schema; positive integer generation/step/version, excluding bools;
  generation/kind/version relationships; matching task hash; timezone-aware commit timestamp.
- Initialized Project State, positive revision, nonnegative review-operation boundary, required
  DESIGN/WORK/DEVIATIONS/counter/recovery shapes; at least two work items.
- Canonical unique sequential IDs and matching counters, supported statuses, nonempty text fields,
  string-list deliverables/evidence, valid deviation references and affected IDs.
- Existing ACTIVE/unfinished, BLOCKED/reason and SUPERSEDED/deviation invariants. The stored
  `recovery.active` must already match; the recovery validator does not silently recompute it.
- Project State's review-operation boundary must match envelope metadata and not exceed the
  retained factual operation counter.
- Handoff is a normalized string of at most 2048 UTF-8 bytes; its digest must match the entire
  State/handoff/metadata envelope. Invalid UTF-8, duplicate JSON fields, NaN and Infinity fail.

Evidence remains opaque Worker-selected material. DONE does not gain an evidence sufficiency or
file-existence requirement. Core performs no semantic judgement or reconciliation from `/work`.

## Handoff lifecycle and bound

The existing Worker HIGH prompt offers a compact operational handoff: where work stopped, current
focus, immediate findings, next concrete action and optional already-performed checks. No rigid
planning schema or semantic quality gate was introduced.

The 2048-byte bound remains raw UTF-8 text length, checked before publication and before trimming.
Exactly 2048 ASCII bytes or 1024 two-byte Cyrillic characters are accepted. One additional byte
is rejected without replacing the old committed generation. Unencodable surrogate text is now
reported as a structural AgentError. The optional field defaults to empty text; omission
replaces the previous handoff with empty text; the prompt still requests that the Worker supply it.

On HIGH success, State and handoff are committed together before history reset. The first resumed
Worker receives the matching committed handoff after State/Map, once per prompt, outside Recent
History. It is not appended on startup, copied into durable State, or combined with reasoning-loop
markers. Only the current envelope/value is loaded; existing exact-action audit archives remain.

## Strict startup algorithm and failure policy

The runtime opts into `StateStore(..., cold_restart=True)` **before** constructing ExperimentRecorder,
ProjectMap or ChatProvider. Low-level `StateStore` inspection reads current schema files without
rolling working State back; it does not authorize a runtime cold restart. Every production
`run_agent` entry uses the strict path. No StateStore API migrates old schema files.

1. Previously nonexistent state directory: create normal fresh controller state. No recovery point
   exists until a valid `project_init` atomically commits the initial generation.
2. `--reset-state`: intentionally create fresh controller state, with no checkpoint/handoff, using
   existing reset semantics. `/work` is preserved. A subsequent valid initialization commits the
   first generation.
3. Existing state directory, no reset: read **only** `state.json`; validate its committed envelope
   and task identity. Missing/invalid committed Project State is `RECOVERY FAILED`.
4. Restore the exact envelope State and matching handoff into the live working fields. No model
   reasoning, logs, Map, handoff-only file or filesystem contents are consulted to invent State.
5. Preserve the factual operation ledger/counter, including post-checkpoint records, so OP IDs
   cannot be reused. Restore the checkpoint's review boundary unchanged. Mark the new process
   `running`; if the working fields/process flag differ, publish this restoration atomically.
   The committed generation itself is not advanced or rewritten.
6. Continue existing startup: refresh deterministic Map from current `/work`, construct the Worker
   prefix and present matching State/handoff. Recent History starts empty as before.

Policy is fail-closed, with **no automatic fallback**: corrupt committed data fails even if a valid
old-looking temp file exists. Incomplete candidates beside a valid committed file are ignored.
A nonexistent or completely empty state directory starts a new run rather than recovery.
A nonempty state directory requires a valid committed state: an uninitialized controller file,
metadata/handoff alone, or useful project files cannot authorize recovery. No `project_init` request or provider call occurs
on failed recovery, and the failed state directory's contents are left intact.

Older controller files without a `recovery_checkpoint` envelope are explicitly rejected by runtime
cold restart. The former format does not retain an unambiguous last-HIGH State paired with its
handoff after working updates; automatic migration would guess a recovery point. No such migration
or reconstruction is implemented. `--reset-state` remains an intentional fresh start, not a repair
or continuation requirement for valid new-format generations. Same task text is still required.

## Current project world and Map

Files modified/created after checkpoint remain intact. Loading older committed State does not roll
back `/work`, rewrite State to agree with files, or grant handoff authority over filesystem reality.
The Worker inspects current evidence and chooses continuation. Map is not part of the generation:
it retains its existing deterministic Python index/cache and refreshes from the current world after
successful State recovery. Missing/stale/corrupt Map cache may be rebuilt only after valid State
has authorized recovery; it cannot substitute for State.

## Interaction with other lifecycle mechanisms

- Count/context HIGH enter the same existing gate and publish via accepted `project_review_complete`.
  Frozen State/Map refresh and full-history reset still follow successful publication. Rejected
  checkpoint validation leaves history intact. The context-budget guard was not modified.
- Periodic review still permits `project_update`/`project_review_skip`, acknowledges operation count,
  preserves the frozen prompt/history, and never publishes or increments the committed envelope.
- Reasoning-loop off/observe/recover (default off at the time of this audit;
  now recover), detector, HTTP interruption, diagnostic-only
  partial reasoning, one-turn marker and three retry attempts were not changed. Loop interruption
  alone cannot publish a generation or replace handoff.
- Positive max-steps and `-1` unlimited are unchanged. No watchdog is used as a checkpoint trigger.
- DNS/bwrap isolation, deterministic output admission, Worker evidence authority and raw-reasoning
  compatibility no-op code were not modified.

## Failure injection and real-process verification

17 new test methods (with multiple boundary/structural subcases) cover:

| Boundary or property | Verified result |
| --- | --- |
| Failure while constructing candidate State | Old committed generation remains recoverable |
| Partial candidate State write | Incomplete temp ignored; old State/handoff load |
| State candidate written, partial handoff write | Incomplete temp ignored; old pair load |
| Complete fsynced candidate, before replace | Old pair loads; complete uncommitted candidate is ignored |
| Failure immediately after replace | New complete pair loads; no mixture |
| Directory fsync error after replace | New visible complete pair loads; no rollback claim |
| Successful HIGH | New matching generation loads and replaces handoff |
| Corrupt candidate vs corrupt committed image | Candidate ignored; corrupt authoritative image fails |
| No generation, even with useful `/work`, Map/logs/handoff | Explicit failure, no provider/Map initialization |
| Mixed State/handoff/version/operation metadata | Integrity/structure validation rejects mismatch |
| Structurally invalid Project State, even with recalculated digest | Explicit failure, no repair |
| 2048-byte boundaries and invalid encoding | Exact limit accepted; excess rejected without publication |
| Initialization, explicit reset, unlimited steps | Existing intended lifecycle retained |
| Periodic review and reasoning-loop recovery | Committed generation/handoff not advanced by these mechanisms |

`tests/fixtures/atomic_runtime_process.py` executes the production runtime in a **fresh Python
interpreter**; only provider responses and shell command effects are scripted. Production StateStore,
publication, startup and deterministic ProjectMap execute normally.

Process A initializes, creates `persisted.py`, commits HIGH/handoff, creates `newer.py`, persists
an additional working State finding, then exits via `os._exit(71)` before finishing. Process B
is a separately launched interpreter with the same task/work/state and no reset. Its first request
contains exactly the committed State/handoff, omits the later working finding, and includes newer
files in refreshed Map. It verifies both files, updates work status and finishes without a second
`project_init`. The operation ledger and newer files survive.

A second set of real-interpreter tests starts a new HIGH and terminates with `os._exit(72)` during
partial State write, partial handoff write, before replace and immediately after replace. A further
fresh interpreter resumes each case and sees respectively the old or new complete pair. These
are real process-exit/restart tests with a mocked model, not solely serialization round trips and
not a live LLM dogfood rerun.

## Exact validation results

| Command | Result |
| --- | --- |
| `.venv/bin/python -m unittest discover -s tests` before edits | 146 tests, OK, 3 restricted-environment skips |
| `.venv/bin/python -m unittest tests.test_atomic_checkpoint_recovery` final | **17 tests, OK, no skips** |
| `.venv/bin/python -m unittest discover -s tests` after implementation | 163 tests, OK, 3 restricted-environment skips |
| `PAVLUSHA_LIVE_NETWORK=1 .venv/bin/python -m unittest discover -s tests` with approved permissions | **163 tests, OK, no skips** |
| `python3 -m unittest discover -s tests` final on system Python | 163 tests, OK, 8 dependency/network permission skips |
| `.venv/bin/python -m compileall -q agent.py bench.py pavlusha_agent tests tools` | Exit 0 |
| `.venv/bin/python agent.py --help` | Exit 0; existing CLI preserved |

The approved full run exercised real bwrap DNS/urllib/network-off isolation and actual local HTTP/SSE
reasoning recovery. The existing unsupported `--history-low` argparse diagnostic is from a passing
compatibility test. No existing test expectations were changed. No additional configured static
checker was found; customary compileall sanity was used.

## Atomicity and durability limits

Logical atomic publication is one same-directory rename of a fully written file. Tests prove process
failure/restart behavior at the boundaries above on this filesystem. File and containing-directory
fsync now request persistence of data and the final directory entry. A failure before replace leaves
the previous committed image; a failure after replace may have committed the new one.

This is not a tested universal power-loss guarantee. Durability depends on the OS/filesystem/storage
honoring fsync/rename. Newly created ancestor-directory entries are not individually fsynced, audit
logs and arbitrary `/work` writes are not covered by this protocol, and no physical power-loss or
faulty-storage test was performed. A missing/corrupt authoritative file after storage failure still
fails recovery rather than guessing. There is no backup-generation fallback or database/journal
framework. One controller writer per state directory remains the existing assumption; concurrent
controllers are not newly supported. The envelope digest does not authenticate hostile edits or
prove Worker evidence true. No new live model inference was run.

## Files changed

- `pavlusha_agent/checkpoint.py` — new bounded generation envelope preparation/validation.
- `pavlusha_agent/state_store.py` — committed snapshot publication, strict startup and directory fsync.
- `pavlusha_agent/project_state.py` — persisted structural validator and deterministic UTF-8 rejection.
- `pavlusha_agent/runtime.py` — opt into strict cold startup (one call-site change).
- `tests/test_atomic_checkpoint_recovery.py` — 17 focused failure/restart regressions.
- `tests/fixtures/atomic_runtime_process.py` — actual-interpreter lifecycle/abrupt-exit fixture.
- `README.md` — usage and strict recovery policy.
- `PROJECT_STATE.md` — committed versus working State semantics.
- `docs/atomic-checkpoint-recovery.md` — this report.
