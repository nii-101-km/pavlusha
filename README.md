# P.A.V.L.U.S.H.A. ☝️😐

<p align="center">
  <img src="assets/pavlusha.png" alt="P.A.V.L.U.S.H.A. mascot holding a jar of jam" width="280">
</p>

Persistent Autonomous Verification & Local Utility
Shell-Handling Agent

P.A.V.L.U.S.H.A. is a deliberately small local agent runtime. One Worker model
chooses actions; the Core/controller validates runtime invariants, executes those
actions in Linux bubblewrap, and maintains durable Project State. The Worker uses
an OpenAI-compatible endpoint; LM Studio is the documented local provider.

## What it does

- Executes Worker-selected shell operations against a persistent work directory.
- Maintains a project plan, navigation snapshots and chronological action history.
- Commits recovery checkpoints and supports bounded context/reasoning-loop recovery.
- Optionally operates a private GUI and asks one text-only Expert for advice.
- Presents progress through an append-only `--live` terminal log.

## Architecture

```text
Local Worker -> selected action -> Core/controller -> shell / optional GUI
                                  |              -> optional Expert advice
                                  +-> Project State, Map, History and checkpoints
```

**Project State** is controller-owned durable state, not a transcript; Core checks
structure and transitions, not whether Worker-selected evidence proves a claim.
**Project Map** is a deterministic navigation snapshot of Python symbols/ranges.
**Recent History** retains reasoning → action → result after the checkpoint.
The prompt is immutable SYSTEM/TASK → frozen MAP/STATE → optional HANDOFF → HISTORY;
the operation ledger and exact history archives remain outside the prompt.
There is no active State Manager, semantic compactor or separate RAW reasoning buffer.

## Requirements

Use Linux, Python 3.12 as the documented tested environment, and a running Worker
provider with a loaded model that follows structured actions. GUI observations
require an image-capable Worker. All Python dependencies are in `requirements.txt`:
Tree-sitter/Python grammar, Rich, python-xlib and Pillow, with declared version ranges.

**System dependencies:** `bubblewrap` (`bwrap`); GUI additionally needs `Xvfb`.
The browser helper requires `epiphany-browser`, `dbus-daemon` (including
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
  --worker-context-budget 117248 --max-tokens 8192 --max-steps -1 \
  --project-map on --history-context-high 0.85 --history-high 30 \
  --project-review-every 10 --command-timeout 300 \
  --reasoning-effort low \
  --interactive --live \
  "Inspect this project, run its existing tests, and report concrete failures."
```

Match the model and context budget to your loaded model; omit `--worker-context-budget`
for LM Studio's automatic context discovery. Effort values are provider-defined;
omit `--reasoning-effort` to retain its default. The State directory must remain outside
`--workdir`. Use a separate State directory for each new TASK; retain it and the same TASK
text when recovering an interrupted run.

**One invocation executes one atomic TASK, and its Project State belongs to that TASK.**
Interactive communication/intervention is available while it is active: press **Ctrl+Z**,
wait for **PAUSED — safe to type**, then enter a message or press empty Enter to resume.
Accepted **FINISH terminates the runtime** in both modes.

Network access is opt-in: add `--network` only when the task needs it, such as downloading
missing dependencies. It is intentionally omitted from the generic example.

The default Worker endpoint is `http://127.0.0.1:1234/v1`. Select a loaded model with
`--model` or `AGENT_MODEL`; otherwise the provider is queried for models.
`--base-url`/`AGENT_BASE_URL` and `AGENT_API_KEY` configure endpoint/authentication.
`example.env` documents variable names and is **not loaded automatically**.
Set credentials in the controller environment, without committing their values.

## Main controls

| Control | Meaning |
| --- | --- |
| `--workdir PATH` / `--state-dir PATH` | Persistent work files / separate controller State. Default State: `<workdir>.pavlusha-state`. |
| `--interactive` | Communication and safe intervention within one active TASK; FINISH terminates the runtime. |
| `--reasoning-effort STRING` | Worker provider passthrough; omitted by default. |
| `--worker-context-budget N` | Override context capacity; omission discovers the loaded LM Studio context length. |
| `--project-map on` | Frozen checkpoint navigation snapshot; ordinary steps do not refresh it. |
| `--project-review-every 10` | Periodic review after executed shell operations; `0` disables it. |
| `--history-high 30` | Request a fresh HIGH checkpoint at 30 retained Worker steps. |
| `--history-context-high 0.85` | Request HIGH when the last successful provider prompt usage reaches this fraction of context capacity. |
| `--max-steps 60` | Step watchdog; `-1` allows unlimited Worker turns. |
| `--max-tokens 8192` | Worker completion ceiling, including reasoning. |
| `--command-timeout 300` | Maximum seconds per shell command; Worker-selected timeouts are clamped to this ceiling. |
| `--output-limit 24000` | Hard stdout/stderr admission limit. |
| `--network`, `--gui`, `--live` | Enable network permission, private GUI tools and terminal presentation. |

`--history-window`, `--raw-reasoning-limit`/`--worker-reasoning-attractor-tokens`,
and legacy State Manager/semantic-compaction/self-context-maintenance switches are
compatibility no-ops in the active loop. There is no active partial LOW history cut;
`--history-low` is unsupported. See `--help` for the accepted CLI surface.

## Safety and private data

The agent executes model-selected operations and can modify its work directory.
Use a dedicated directory and review the task, model and environment. Bubblewrap
isolation depends on host namespace policy and mounts; it is **not an absolute
security boundary**. The self-test checks basic local assumptions, not every escape.

`--network` allows requested network operations and shares the host network, including
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

## Optional GUI

`--gui` enables a private 800×600 Xvfb display without exposing the host desktop.
The application runs in the same bubblewrap work environment. Coordinate actions
include view, click, right-click, drag and literal text input. Screenshots are
transient observations; mouse markers show executed gestures, not semantic success.

Keep the main GUI command in the foreground. Its session and any server started
inside that command persist across idle/model/shell/review turns until `gui_close`
or controller termination/recovery. `gui_start.timeout` is a compatibility no-op;
GUI does not extend ordinary shell process lifetime or count as shell operations.
See [GUI tools](docs/gui-tools.md) for actions, delays, annotation and cleanup.

## Optional Expert

Expert is optional and off by default: one independent text-only consultant, not
another autonomous Worker. Enable it with an explicit endpoint/model and `--network`
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
A nonexistent State directory starts normally; `--reset-state` intentionally starts
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

`--reasoning-loop-recovery off|observe|recover` defaults to `off`. Observation and
recovery use deterministic lexical detection; `observe` does not interrupt the Worker.
`recover` interrupts a confirmed loop and retries the exact pre-loop request
plus one temporary instruction. Interrupted reasoning stays only in diagnostics,
not History/State/handoff; length or lack of an action alone does not trigger detection.

`--max-reasoning-loop-recoveries 3` bounds retries; another loop on the last retry
stops Core with State/work intact. Accepted runtime actions reset the consecutive
counter; invalid/rejected actions do not. Retries stay inside the same step without
rerunning review/Map preparation. Detection needs `reasoning_content` SSE deltas;
buffered providers delay detection. Closing the response does not acknowledge
cancellation of server computation.

## Live terminal output

`--live` is an append-only Rich-based log on **stderr**, without an alternate screen,
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
python -m compileall -q agent.py bench.py pavlusha_agent tests tools
```

Install `requirements-test.txt` first (controller dependencies plus the independent
JSON Schema validator used by contract tests). Ordinary provider tests use scripted replies,
without paid API calls. Browser/network integration tests are opt-in through
`PAVLUSHA_TEST_GUI_BROWSER=1` / `PAVLUSHA_LIVE_NETWORK=1`; real Xvfb capture runs
when available. OCR smoke tools require a separately supplied OCR application;
local benchmark runs, OCR outputs and downloaded models are not distributed here.

## Documentation

- [Project State](PROJECT_STATE.md): State shape, evidence, review and handoff contracts.
- [Atomic checkpoint/recovery](docs/atomic-checkpoint-recovery.md): committed generations, strict restart, failure tests and durability limits.
- [Interactive chat](docs/interactive-chat.md): safe boundaries, held proposals, reasoning effort and terminal FINISH.
- [GUI tools](docs/gui-tools.md): private display, actions, browser lifecycle and integration checks.

## License

Licensed under the [MIT License](LICENSE).

## Interactive chat (v0.3)

`--interactive` keeps the Worker autonomous within one active TASK and requires terminal
stdin. It enables the existing live output automatically. Press **Ctrl+Z** to request
PAUSE, then wait for **PAUSED — safe to type** before composing an intervention.
A message plus Enter resumes with that input; empty Enter resumes a held proposal
without a message. `/quit` or EOF at the paused prompt ends the session.
**FINISH terminates the runtime**; a new TASK needs a separate invocation and its own State.
Piped task input remains available without interactive mode.

Worker can use `message` to speak and continue, or `wait_for_user` when input is necessary.
See [interactive lifecycle, boundaries, recovery and limitations](docs/interactive-chat.md).

`--reasoning-effort` accepts a string and passes it unchanged in every Worker provider
request, including after `release_worker`. Omission preserves provider/model default
behavior. Values are not locally normalized or replaced: provider rejection remains an
explicit runtime error, with no hidden fallback. This is a request option; the existing
model unload/restore configuration handling is unchanged.
