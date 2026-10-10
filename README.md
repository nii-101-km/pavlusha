# P.A.V.L.U.S.H.A. ☝️😐

<p align="center">
  <img src="assets/pavlusha.png" alt="P.A.V.L.U.S.H.A. mascot holding a jar of jam" width="280">
</p>

Persistent Autonomous Verification & Local Utility
Shell-Handling Agent

Version **0.5.0**.

P.A.V.L.U.S.H.A. is a deliberately small local agent runtime. One Worker model
chooses actions; the Core/controller validates runtime invariants, executes
shell/GUI actions in Linux bubblewrap, and maintains durable Project State. The Worker uses
an OpenAI-compatible endpoint; LM Studio is the documented local provider.

## What it does

- Executes Worker-selected shell operations against a persistent work directory.
- Maintains a project plan, navigation snapshots and chronological action history.
- Commits recovery checkpoints and supports bounded context/reasoning-loop recovery.
- Operates a private GUI when its host dependencies are available.
- Optionally loads trusted Python functions, including DOCX/XLSX packs, and asks
  one text-only Expert for advice.
- Presents progress through an append-only `--live` terminal log.

## Architecture

```text
Local Worker -> selected action -> Core/controller -> shell / private GUI
                                  |              -> optional trusted functions
                                  |              -> optional Expert advice
                                  +-> Project State, Map, History and checkpoints
```

**Project State** is controller-owned durable state, not a transcript; Core checks
structure and transitions, not whether Worker-selected evidence proves a claim.
**Project Map** is a deterministic navigation snapshot of Python symbols/ranges.
**Recent History** retains reasoning → action → result after the checkpoint.
Work files hold the actual deliverables; Project State holds concise decisions,
status and evidence pointers, not full artifacts or reasoning transcripts.
The prompt is immutable SYSTEM/TASK → frozen MAP/STATE → optional HANDOFF → HISTORY;
the operation ledger and exact history archives remain outside the prompt.
There is no active State Manager, semantic compactor or separate RAW reasoning buffer.

## Requirements

Use Linux, Python 3.12 as the documented tested environment, and a running Worker
provider with a loaded model that follows structured actions. GUI observations
require an image-capable Worker. Core Python dependencies are in `requirements.txt`:
Tree-sitter/Python grammar, Rich, python-xlib and Pillow, with declared version ranges.
Office packs have separate optional dependencies described below.

**System dependencies:** `bubblewrap` (`bwrap`); GUI additionally needs `Xvfb`.
Physical keyboard input also requires host libX11/XKB. The browser helper requires
`epiphany-browser`, `dbus-daemon` (including
`dbus-run-session`) and `libglib2.0-bin` on Debian/Ubuntu. These are host packages,
not pip dependencies. Expert needs no additional Python SDK.

## Quick start

Install bubblewrap through your system package manager and start the Worker provider.
Then, from the repository directory:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python agent.py --help
python agent.py --workdir ./agent-work --self-test-bwrap
```

To run a task against an existing project, replace `project_dir` with its absolute path:

```bash
project_dir="/absolute/path/to/your/existing-project"
python agent.py \
  --workdir "$project_dir" \
  --state-dir "${project_dir}.pavlusha-state/task-001" \
  --model qwen/qwen3.8-27b \
  "Inspect this project, run its existing tests, and report concrete failures."
```

Match `--model` to your loaded model. LM Studio's context capacity is discovered
automatically; use `--worker-context-budget N` to override it. Effort values are provider-defined;
omit `--reasoning-effort` to retain its default. The State directory must remain outside
`--workdir`. Use a separate State directory for each new TASK; retain it and the same TASK
text when recovering an interrupted run.

**One invocation executes one atomic TASK, and its Project State belongs to that TASK.**
Interactive communication/intervention is available while it is active: press **Ctrl+Z**,
wait for **PAUSED — safe to type**, then enter a message or press empty Enter to resume.
Accepted **FINISH terminates the runtime** in both modes.

Live output, network permission, Project Map and interactive mode are enabled by
default, alongside GUI and lexical reasoning-loop recovery. Use `--no-live`,
`--no-network`, `--project-map off` or `--no-interactive` to opt out. Networked
shell/GUI actions must still request `network:true`. Functions and Expert remain opt-in.
Default interactive mode requires terminal stdin; use `--no-interactive` for pipes/batch.
Unavailable Project Map dependencies produce a diagnostic and leave the runtime usable
without a map. The default step limit is unlimited (`--max-steps -1`).

The default Worker endpoint is `http://127.0.0.1:1234/v1`. Select a loaded model with
`--model` or `AGENT_MODEL`; otherwise the provider is queried for models.
`--base-url`/`AGENT_BASE_URL` and `AGENT_API_KEY` configure endpoint/authentication.
`example.env` documents variable names and is **not loaded automatically**.
Set credentials in the controller environment, without committing their values.

## Main controls

| Control | Meaning |
| --- | --- |
| `--workdir PATH` / `--state-dir PATH` | Persistent work files / separate controller State. Default State: `<workdir>.pavlusha-state`. |
| `--interactive` / `--no-interactive` | Default-on communication and safe intervention within one active TASK; FINISH terminates the runtime. |
| `--reasoning-effort STRING` | Worker provider passthrough; omitted by default. |
| `--functions MODULE.py` | Repeatable: trusted local Python modules with globally unique function names; synchronous and not sandboxed. See [custom functions](docs/custom-functions.md). |
| `--worker-context-budget N` | Override context capacity; omission discovers the loaded LM Studio context length. |
| `--project-map on` | Default frozen checkpoint navigation snapshot; `off` disables; ordinary steps do not refresh it. |
| `--project-review-every 10` | Periodic review after executed shell/function operations; `0` disables it. |
| `--history-high 200` | Request a fresh HIGH checkpoint at 200 retained Worker steps; the existing context trigger can fire earlier. |
| `--history-context-high 0.85` | Request HIGH when the last successful provider prompt usage reaches this fraction of context capacity. |
| `--max-steps -1` | Default unlimited Worker turns; a positive value enables the existing step watchdog. |
| `--max-tokens 8192` | Worker completion ceiling, including reasoning. |
| `--command-timeout 300` | Maximum seconds per shell command; Worker-selected timeouts are clamped to this ceiling. |
| `--output-limit 24000` | Hard shell-output and serialized function-result admission limit, in characters. |
| `--no-network`, `--no-gui`, `--no-live` | Disable default network permission, private GUI tools or Worker progress output. Use the corresponding positive flags to enable them explicitly. |

Only active runtime options are accepted; obsolete State Manager, context-maintenance,
RAW-reasoning and partial history-window switches and aliases have been removed.
See `--help` for the current CLI. `agent.py` is only a command-line entry point;
Python callers should import the relevant `pavlusha_agent` module directly.

Controller state uses schema **3**. Older state formats are rejected without migration.
Use a new `--state-dir` for a new run, or `--reset-state` to explicitly start fresh
controller state while preserving work files. Older runs cannot be resumed with this version.

## Optional document packs

The [DOCX pack](docs/docx-functions.md) provides structure inspection, compact
paginated reading, exact text replacements preserving runs, and PDF/PNG rendering.
It represents supported Word equations (OMML) as text and flags incomplete
representations; it is not a formula editor or universal document reader.
The [XLSX pack](docs/xlsx-functions.md) provides sheet/layout inspection,
bounded range reading, point edits, and LibreOffice recalculation/print rendering.
Formula text and cached values are distinct; caches may be missing or stale.
`openpyxl` does not calculate formulas.

Install only the optional dependencies you need in the controller environment:

```bash
pip install -r tools/requirements-docx.txt
pip install -r tools/requirements-xlsx.txt
# Additionally, for XLSX rendering:
pip install -r tools/requirements-xlsx-render.txt
```

DOCX reading/editing uses `lxml`; its requirements file also includes fixture/helper
dependencies. XLSX reading/editing uses `openpyxl` and `lxml`. Standard rendering
needs host LibreOffice (`soffice`), Poppler (`pdfinfo`, `pdftoppm`) and suitable fonts.
DOCX rendering works without manual renderer configuration. LibreOffice is optional
for the runtime and for both packs' reading/editing; nothing is installed automatically.

Place input documents in `document-work`, then load either or both packs:

```bash
python agent.py --workdir ./document-work \
  --functions tools/docx_functions.py \
  --functions tools/xlsx_functions.py \
  "Inspect report.docx and figures.xlsx; prepare amended copies and verify them."
```

Both packs use `--workdir` as their only document root, reject absolute paths,
`..` and symlink escapes, and bind reads/edits to source revisions. Edits produce
new files without overwriting the source. Semantic decisions and visual evaluation
remain with Worker. Rendering creates files, not an automatic visual observation.

## Safety and private data

The agent executes model-selected operations and can modify its work directory.
Use a dedicated directory and review the task, model and environment. Bubblewrap
isolation depends on host namespace policy and mounts; it is **not an absolute
security boundary**. The self-test checks basic local assumptions, not every escape.

Custom function modules execute trusted Python with controller permissions and
host filesystem, environment and network access; they are **not sandboxed** by
bubblewrap or `--no-network`. Keep modules outside the Worker-writable workdir.
Calls are synchronous with no generic timeout/cancellation or exactly-once guarantee;
`--command-timeout` does not bound them. Pack path checks are not a sandbox against
malicious code or concurrent filesystem races. See [custom functions](docs/custom-functions.md).

Default network permission (`--no-network` disables it) allows requested network operations and shares the host network, including
loopback services; shell/GUI actions must still request `network=true`. Networked
shell commands resolve `/etc/resolv.conf` anew and mount its target read-only when
it is a symlink, without exposing host `/run`/sockets or writing resolver copies
into `/work`. A missing resolver fails explicitly. Network-off commands retain
`--unshare-all` with no added resolver mount.

Sandbox environments are cleared. Keep custom State directories outside `/work`.
Work files, State, reasoning/history diagnostics and GUI captures may contain private
task material; default ignore rules do not cover arbitrary custom output paths.
The GUI browser disables WebKit's nested sandbox and relies on outer bubblewrap.
Expert sends selected text to its configured API and may incur provider charges.

## Private GUI

GUI actions are enabled by default when Xvfb, python-xlib and Pillow are available.
`--no-gui` disables them; `--gui` remains accepted. Missing prerequisites disable
GUI with a diagnostic while shell/functions continue. The private 800×600 Xvfb
display starts when `gui_start` is used, without exposing the host desktop.
The application runs in the same bubblewrap work environment. Actions include
`gui_start`, `view_gui`, `click`, `right_click`, `drag`, `type_text`, `press_key`,
`hold_key` and `gui_close`. Each successful input action waits its selected delay
and supplies a fresh screenshot for the next Worker request. Screenshots are
transient observations; mouse markers show executed gestures, not semantic success.

`type_text` enters literal printable text. `press_key` taps a supported physical
key, optionally with `ctrl`/`shift`/`alt`/`super`; `hold_key` holds one key for
`0 < duration <= 2` seconds. Keys are US-QWERTY physical positions from the private
XKB map, without changing layout. Applications using translated symbols can still
interpret W as `ц`; a hold does not guarantee repeated movement. Cleanup releases
keys after completion and controlled errors; Core retries release after helper
failure and closes the private display if release cannot be confirmed.

Keep the main GUI command in the foreground. Its session and any server started
inside that command persist across idle/model/shell/review turns until `gui_close`
or controller termination/recovery. `gui_start` accepts command, network and delay;
its removed timeout field is rejected. GUI does not extend ordinary shell process
lifetime or count as shell operations.
See [GUI tools](docs/gui-tools.md) for actions, delays, annotation and cleanup.

Document rendering creates PDF/PNG files. GUI opens those files in a viewer;
`view_gui` sends a screenshot to Worker. Understanding that screenshot requires
an image-capable Worker/backend. Rendering alone does not provide visual evidence.

## Optional Expert

Expert is optional and off by default: one independent text-only consultant, not
another autonomous Worker. Enable it with an explicit endpoint/model and network permission
(even for local endpoints). Calls may incur provider charges.

```bash
# Set MY_EXPERT_KEY in the controller environment using your secret manager.
python agent.py --workdir ./agent-work --network \
  --expert on --expert-base-url https://api.openai.com/v1 \
  --expert-model gpt-4.1 --expert-api-key-env MY_EXPERT_KEY "TASK TEXT"
```

`ask_expert` sends only the Worker-selected plain-text question/context and a fixed
consultant instruction; question must be non-empty, context may be empty. No
History/State/Map/files/images/reasoning are automatically supplied. Advice returns
as `EXPERT RESULT` in History. Expert cannot execute actions, mutate/publish State/Map
or finish the task. Existing initialization, periodic-review and HIGH gates apply;
the synchronous call keeps GUI alive.

Keys are read at each call from `--expert-api-key-env` (default `EXPERT_API_KEY`);
an unset key permits unauthenticated compatible endpoints. Credentials are excluded
from prompts/State/telemetry. Endpoint URLs reject credentials, queries and fragments.
Defaults: `--expert-max-tokens 4096`, `--expert-max-calls 5`, `--expert-timeout 60` seconds.
The call ceiling is per controller run and includes failed API attempts. Failures
return bounded tool results, without triggering Worker cold recovery or terminating
the task. Optional `--expert-reasoning-effort` is passed unchanged; unsupported controls
produce a tool error. Calls have no automatic retries or fallback.

The default transport is `chat-completions`. Perplexity Agent API is also supported:
select `--expert-transport perplexity-agent`, `--expert-base-url https://api.perplexity.ai/v1`,
a supported `--expert-model`, and `--expert-api-key-env PERPLEXITY_API_KEY`.
Provider-reported Expert usage is separate from Worker HIGH accounting; retained
advice contributes to later Worker prompts as ordinary tool output.

## Recovery and context

Ordinary shell actions may also opt into `"gpu":true` for NVIDIA compute device access
inside the existing sandbox. This option is independent of `release_worker`; either can
be used alone or together. Omission/false leaves the sandbox unchanged. See
[GPU shell access](docs/gpu-shell.md) for the exact device boundary and timeout ceiling.

Shell actions can opt into `"release_worker": true`, for example
`{"action":"shell","command":"python workload.py","timeout":120,"release_worker":true}`.
The Worker chooses `timeout` before release; Core clamps it to `--command-timeout`.
Core commits the current materialized Project State using the existing atomic checkpoint
transaction, releases the backend, runs the ordinary bounded shell command, and restores
the backend in `finally`. It then cold-recovers from that checkpoint and current files,
with the normal shell result and no previous History or inference/KV state. Materialize
durable changes with `project_update` before selecting this mode. Omitted/false release
keeps existing shell behavior. See [Worker release](docs/worker-release.md) for backend limits.

Periodic review acknowledges or updates working State without refreshing frozen
snapshots or clearing History. Successful HIGH commits reviewed State and a bounded
handoff, refreshes Map/State snapshots, archives all prior History and starts empty.
Rejected reviews preserve History; ordinary State updates do not replace the frozen
prompt snapshot. Archives are never automatically restored into Worker context.

Restart with the **same task, workdir and state-dir**, without `--reset-state`. Cold
startup loads only the validated committed generation/handoff, preserves the factual
operation ledger and newer work files, rebuilds Map, and discards post-checkpoint
working State/history. Missing/invalid committed data fails as **RECOVERY FAILED**;
there is no reconstruction from logs/files, legacy migration or backup fallback.
A nonexistent or completely empty State directory starts normally; `--reset-state` intentionally starts
fresh controller State while preserving work files. State is atomically committed;
it does not guarantee arbitrary work-file durability or universal power-loss recovery.
See [Project State](PROJECT_STATE.md) and [atomic recovery](docs/atomic-checkpoint-recovery.md).

Context pressure uses provider-reported `usage.prompt_tokens`, not a byte/token
predictor; unknown usage is not guessed and completion/reasoning usage is separate.
Only LM Studio's structured `exceed_context_size_error` (HTTP wrapper or SSE error)
triggers strict cold recovery: committed State/handoff, preserved ledger, fresh Map
and empty History. Before an initial checkpoint, or on a second overflow against
the same generation in one runtime invocation, recovery fails closed. Other
provider/transport errors propagate. Work files remain current.

### Reasoning-loop observation and recovery

`--reasoning-loop-recovery off|observe|recover` defaults to `recover` in both
interactive and non-interactive runs. Explicit `off` disables detection. Observation and
recovery use deterministic lexical detection; `observe` does not interrupt the Worker.
`recover` interrupts a confirmed loop and retries the exact pre-loop request
plus one temporary instruction. Interrupted reasoning stays only in diagnostics,
not History/State/handoff; length or lack of an action alone does not trigger detection.

`--max-reasoning-loop-recoveries 3` bounds retries; another loop on the last retry
enters `NEED USER` in interactive mode, through the existing safe input boundary.
Only nonblank user guidance resumes the same TASK and resets this recovery counter;
empty input keeps waiting, and `/quit` or EOF ends the session. State/work remain intact.
With `--no-interactive`, exhaustion stops with a bounded error explaining that human
escalation is unavailable. Checkpoint/State integrity failures still fail closed.
Accepted runtime actions reset the consecutive counter; invalid/rejected actions do not.
Retries stay inside the same step without
rerunning review/Map preparation. Detection needs `reasoning_content` SSE deltas;
buffered providers delay detection. Closing the response does not acknowledge
cancellation of server computation.

Malformed actions and rejected Project State mutations share a limit of three consecutive failures.
An accepted action resets this counter. Shell operation records preserve elapsed time as `duration_seconds`.

Completed shell and `call_function` operations also have an always-on exact repetition guard.
It hashes canonical JSON of the action and its result (excluding shell elapsed time) and checks
the last nine operations for cycles of length 1, 2 or 3 repeated three times. The last result is
recorded before entering the ordinary interactive pause; no next model request is sent until
the user resumes. Empty Enter resumes, guidance is forwarded normally, and `/quit` ends the run.
Batch mode stops with an error instead of waiting. There is no sleep timer or external checker.
GUI actions and Expert consultations clear this operation history; so does user intervention.
Project State updates/reviews do not clear it. Withheld or unexecuted results cannot establish a repeat.
This is a repetition alarm, not proof of stalled progress: polling and silent commands that change
files can trigger it; different output, including timestamps, can hide a real loop.

In interactive mode, `Ctrl+\\` cancels the current generation and enters the ordinary pause.
Partial reasoning/actions stay in diagnostics and are never executed or sent back as context.
During tools it requests a safe pause after completion. `Ctrl+Z` keeps its existing safe-pause
behavior. Cancellation closes the HTTP stream at the next received line; a stalled connection or
prompt processing can delay it. The installed LM Studio backend was observed cancelling its task
and releasing the compute slot on disconnect; other servers may continue computing.

At PAUSED, `/trim_last_turns N` (also `trim_last_turns N`) removes N completed turns from active
context only after the displayed warning and explicit `y` confirmation. The default is refusal.
It stays paused afterwards for guidance or Enter. Whole reasoning/action/result groups are removed;
human instructions, original task and system instructions remain. Removed records are archived;
Project State, /work and executed effects are not rolled back. Current Project State is refreshed
in the prompt, stale GUI observation is cleared and old prompt-usage measurement is discarded.
Normal cold resume continues from the existing committed checkpoint, without replaying History
archives; this command adds no new checkpoint or recovery format.

## Live terminal output

Default live output (`--no-live` disables Worker progress) is an append-only Rich-based log on **stderr**, without an alternate screen,
widgets or interaction. Commands/headings are bold; shell/State cyan, Expert/GUI
magenta, success green, warnings yellow, errors red, reasoning/metadata subdued.
Rich renders Markdown for COMPLETE summaries and Expert answers only; reasoning,
actions, evidence and shell output stay literal, including partial Markdown/whitespace.
Expert events show requested → started → returned/failed, then Worker resumption;
private requests/credentials are not displayed. Presentation does not alter canonical data.

Colors need a TTY, a non-dumb terminal and no `NO_COLOR` (even empty). Redirected
stderr never gets ANSI, even with `FORCE_COLOR`; Markdown remains readable. Capture
with `>run.txt 2>&1` and keep the transcript private. Without Rich, rendering falls
back to literal prose; install requirements for full Markdown presentation.

## Tests

Worker final actions use native JSON Schema constrained generation while Core
retains validation and permission checks. See [Worker action contract](docs/worker-action-contract.md)
for the existing action families, phase selection, truncation handling and local verification.

```bash
python -m unittest discover -s tests
python -m compileall -q agent.py pavlusha_agent tests tools
```

Install `requirements-test.txt` first (controller dependencies plus the independent
JSON Schema validator used by contract tests). Ordinary provider tests use scripted replies,
without paid API calls. Browser/network integration tests are opt-in through
`PAVLUSHA_TEST_GUI_BROWSER=1` / `PAVLUSHA_LIVE_NETWORK=1`; real Xvfb capture runs
and physical-key tests run when their host tools are available. Office tests may
skip without optional dependencies; real render tests use
`PAVLUSHA_TEST_REAL_RENDER=1` / `PAVLUSHA_TEST_REAL_XLSX_RENDER=1` with the corresponding
`tests.test_docx_pack` / `tests.test_xlsx_pack` modules and system tools installed.
OCR smoke tools require a separately supplied OCR application;
OCR outputs and downloaded models are not distributed here. The retired context-maintenance
A/B benchmark and its obsolete smoke launcher have been removed; checkpoint/context
regressions remain in the unit suite and `tools/replay_context_guard.py`.

## Documentation

- [Project State](PROJECT_STATE.md): State shape, evidence, review and handoff contracts.
- [Atomic checkpoint/recovery](docs/atomic-checkpoint-recovery.md): committed generations, strict restart, failure tests and durability limits.
- [Interactive chat](docs/interactive-chat.md): safe boundaries, held proposals, reasoning effort and terminal FINISH.
- [GUI tools](docs/gui-tools.md): private display, actions, browser lifecycle and integration checks.
- [Custom functions](docs/custom-functions.md): module loading, typed contracts and trust/recovery limits.
- [DOCX pack](docs/docx-functions.md) and [XLSX pack](docs/xlsx-functions.md): dependencies, reading/editing/rendering and coverage limits.

## License

Licensed under the [MIT License](LICENSE).

## Interactive chat

Default interactive mode keeps the Worker autonomous within one active TASK and requires terminal
stdin. `--no-interactive` disables chat; `--no-live` disables Worker progress while
retaining chat messages and pause controls. Press **Ctrl+Z** to request
PAUSE, then wait for **PAUSED — safe to type** before composing an intervention.
A message plus Enter resumes with that input; empty Enter resumes a held proposal
without a message. `/quit` or EOF at the paused prompt ends the session.
**FINISH terminates the runtime**; a new TASK needs a separate invocation and its own State.
Piped task input remains available with `--no-interactive`.

Worker can use `message` to speak and continue, or `wait_for_user` when input is necessary.
See [interactive lifecycle, boundaries, recovery and limitations](docs/interactive-chat.md).

`--reasoning-effort` accepts a string and passes it unchanged in every Worker provider
request, including after `release_worker`. Omission preserves provider/model default
behavior. Values are not locally normalized or replaced: provider rejection remains an
explicit runtime error, with no hidden fallback. This is a request option; the existing
model unload/restore configuration handling is unchanged.

Reasoning detector details are hidden by default; use `--reasoning-loop-diagnostics` to display
window sizes, similarity and distance. Detection and recovery remain enabled; full interruption
diagnostics remain in `experiment.jsonl`. Fenced backtick code blocks in reasoning are excluded
from lexical comparison; prose and inline code remain checked. Closing backtick fences must be
at least as long as their opener and have no language tag. An unclosed fence stays excluded
for the rest of that generation.
