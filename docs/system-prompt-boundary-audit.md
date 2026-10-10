# Worker system-prompt boundary audit

Audited on 2026-10-07. This change concerns instructions only; it does not add
artifact types, memory, phases, roles, tools or runtime enforcement.

## Actual instruction sources

`pavlusha_agent/config.py:SYSTEM_PROMPT` is the active base contract.
`build_worker_system_prompt` optionally appends `GUI_PROMPT`.
`pavlusha_agent/runtime.py:_run_agent` adds the effective network permission,
the checkpoint Project Map notice, optional `EXPERT_PROMPT`, `CHAT_PROMPT`,
and `FunctionRegistry.prompt()` with the enabled functions' declarations.
These form the first, role=system message.

`build_worker_messages` then supplies TASK separately as role=user, followed by
the frozen Map/State/handoff, chronological History and transient observations.
Current-phase HIGH/periodic-review instructions are appended after History.
Context notices and the temporary lexical recovery instruction are also separate
messages, not another system prompt. Phase-dependent `response_format` is a
generation schema, not an instruction source or substitute for Core validation.

The retired State Manager prompt and tools have been removed. The active contract
is defined only by the Worker and enabled capability prompts described above.

## Classification of existing rule groups

SYSTEM means a general Worker responsibility. TASK means concrete requirements
belong to the user. RUNTIME means Core owns the mechanism; a short usage contract
may still be needed by the Worker. REDUNDANT / OVER-SPECIFIED identifies repeated
or unnecessarily particular wording, not permission to remove a runtime boundary.

| Existing group | Classification | Boundary and treatment |
| --- | --- | --- |
| Autonomous execution rather than command suggestions | SYSTEM | Retained; opening now covers available tools rather than defining all work as file editing/testing. |
| Goals, prescribed workflow, required deliverables | TASK | Explicitly assigned to TASK; no domain workflow introduced. |
| Bounded reasoning/history; absence does not prove work was not done | SYSTEM + RUNTIME | Core retains/discards context; Worker recovers from supplied sources and current evidence. Retained. |
| Materializing durable reasoning results | SYSTEM, previously ambiguous | Now distinguishes stable project outcomes in workspace artifacts from concise recovery pointers in State. |
| Project State DESIGN/WORK/DEVIATION | SYSTEM + RUNTIME | Worker selects meaningful content; Core validates structure and publishes it. Full artifact content is excluded from State instructions. |
| Map/State snapshot freshness; later History delta | RUNTIME + SYSTEM | Core builds snapshots/history; Worker must interpret their age. Retained in a shorter paragraph. |
| Files/tests/environment and hypotheses | SYSTEM | Actual observations ground factual claims; reasoning alone is not verification. Kept once. |
| Exactly one complete JSON action | RUNTIME usage contract | Schema/parser/validators enforce it; example protocol retained. Duplicate Markdown-fence prohibition removed. |
| Initial WORK plan, at least two items, one ACTIVE | RUNTIME + SYSTEM | Structural minimum is enforced by `validate_project_action`; Worker still chooses useful objectives. Retained without a mandatory design document or design phase. |
| Final verification as a planned work item when applicable | SYSTEM | Retained; concrete verification method is TASK-dependent. |
| Atomic `project_update`, IDs/statuses/references | RUNTIME usage contract | Examples retained; no prompt-based replacement for structural validation. |
| Material WORK evidence and its sufficiency | SYSTEM + RUNTIME | Worker selects files/operation results; Core normalizes strings and does not prove sufficiency. Retained. |
| BLOCKED reason and SUPERSEDED deviation | RUNTIME usage contract | Validators enforce transitions. Retained. |
| State is not a file inventory | SYSTEM | Existing deliverables/Map distinction retained. |
| Periodic review versus HIGH checkpoint | RUNTIME usage contract | Gates, publication, archival/reset are Core-owned. Removed repeated procedural narration, kept the distinct actions and Worker review obligation. |
| Short handoff, latest-only, non-authoritative | RUNTIME + SYSTEM | Core bounds/publishes it; Worker selects immediate continuation information. Retained; handoff is not a project-content store. |
| Shell network/GPU/release independence and bounds | RUNTIME usage contract | Core enforces permissions and lifecycle. Backend/exclusivity caveats retained so Worker can request tools correctly. |
| Workspace, readonly system paths, controller State isolation | RUNTIME + SYSTEM | Core provides confinement; Worker must respect it. No filesystem authority changed. |
| Non-interactive shell commands, project-local environments | SYSTEM, environment-specific | Appropriate to this local Linux tool contract; retained. |
| Inspect first, smallest sufficient change, useful checks, diagnose failures | SYSTEM | Retained. TASK determines what needs changing and checking. |
| FINISH only after requested work and applicable testing | SYSTEM + RUNTIME | Core rejects unfinished statuses and exits on accepted FINISH; Worker remains responsible for actual completeness and verification. Retained. |
| Repeated ground-truth/evidence rule near the end | REDUNDANT | Removed duplicate; source authority and evidence policy remain above. |
| GUI application/browser commands, dimensions, annotations, transient images | RUNTIME usage contract | Operational details, not TASK workflow. Host-specific restrictions are particular but describe the supported tool path; left unchanged in this scoped patch. |
| Expert consultation, interactive messages, registered functions | RUNTIME + SYSTEM | Availability/transport/gates are Core-owned; interpretation and decisions remain Worker-owned. Existing conditional instruction blocks unchanged. |

## Concrete problem and minimal patch

The old materialization sentence followed “Project State is a durable recovery
checkpoint” and named only design/work/deviations/evidence. It did not say where
full, reusable project results belong. This could encourage State inflation or
leaving substantive outcomes only in transient History. It was a missing
instruction boundary, not evidence of a missing memory mechanism.

The patch:

1. Leaves goals/workflow/deliverables to TASK and describes execution through
   available tools, without prescribing a file-development workflow.
2. Says to save stable results needed later in appropriate `/work` artifacts and
   keep them consistent with accepted changes. This is conditional on their value
   to the work, not a requirement to document every task or every thought.
3. Restricts State content to concise decisions/status/deviations/evidence pointers;
   distinguishes intent/reasoning from execution/verification. It neither changes
   State fields nor adds semantic validation.
4. Condenses duplicate history mechanics and removes two repeated rules while
   retaining checkpoint action instructions and source authority.

Only `SYSTEM_PROMPT` changed in production code. Its size is essentially unchanged:
7284 to 7283 characters (941 to 946 whitespace-delimited words); this is not a token
measurement. All action examples, constraints and optional capability blocks remain.

## Cross-domain check and limits

For a task that explicitly requests concept/story/characters/world/visual design
before implementation, those outcomes become ordinary workspace artifacts; concise
decisions and evidence point to them in State. The task chooses their order and
form. The prompt contains no game-specific terms or file templates. The same rule
applies to research conclusions, application specifications or refactoring decisions.
For a small edit it imposes no preliminary document or additional execution phase.

Artifacts should be produced during ordinary work when the relevant tools are
available, not deferred to HIGH, where Core restricts actions. Checkpoint publication
does not create artifacts, verify their existence or judge their contents. After
cold recovery Worker must inspect current files and reconcile them with committed
State; the patch does not create persistent chat or reconstruct discarded reasoning.

Existing validators, FINISH gates, evidence-normalization, prefix/history,
checkpoint/recovery, interactive and release tests establish compatibility with
runtime mechanics. They do not prove that every model will follow an arbitrary
workflow or correctly decide which outcomes merit an artifact. No such behavioral
claim or new automatic artifact policy is introduced by this prompt-only change.

## Verification

- Existing relevant suites: 89 tests, OK (durable reasoning/State, prefix layers,
  checkpoint snapshots, reasoning/history, structured actions, Worker release,
  Project Map, interactive and human escalation).
- Full discovery: 390 tests, OK, 26 skipped. The initial sandboxed run could not
  start Xvfb in two GUI tests; the successful full run used host permissions.
- Compile/import sanity and `git diff --check`: passed.
- AST comparison against the pre-task `config.py`: only the `SYSTEM_PROMPT`
  assignment value differs. Other constants, optional prompts and assembly code
  are unchanged. All 11 existing action/change examples match the old text.
