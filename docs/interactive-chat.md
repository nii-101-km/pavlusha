# Interactive chat and safe intervention (v0.3)

Enable explicitly with `python agent.py --interactive --workdir ./agent-work "Your task"`.
The existing model, network, GUI, Expert, GPU shell, Worker release and limit options apply.
Interactive mode enables the existing append-only live renderer automatically. Without a
positional task, the first safe input prompt collects it. Ordinary piped stdin is unchanged;
`--interactive` requires terminal stdin and fails explicitly for a pipe rather than waiting.

## Controls and lifecycle

- **Ctrl+Z** during autonomous work requests PAUSE. It is reserved for this session instead
  of suspending the controller through shell job control. The terminal signal handler sets
  one local flag and writes `PAUSE REQUESTED` directly to stderr; it does not call Rich,
  read input, terminate work, or manipulate checkpoint state.
- Core completes the already admitted indivisible operation, then reports **PAUSED — safe
  to type**. No generation, retry or action is admitted while this prompt collects input.
- Type one message and press Enter to resume with that evidence. Empty Enter resumes
  without a message. `/quit` or terminal EOF ends the session at this safe boundary.
- Worker `wait_for_user` enters the same safe input path inside the active TASK.
- Accepted FINISH records the existing finished status, prominently displays its summary,
  and returns 0 from the runtime in both modes. It never enters another input prompt or
  accepts another task. Terminal attributes/signal handlers are restored on exit.

One invocation is one atomic TASK and one Project State lifecycle. Interactive mode supplies
communication/intervention within that task, not a shell for multiple tasks. The existing
`mark_finished` and committed checkpoint behavior are unchanged; no next-task State is created.

Ctrl+Z was chosen because the existing runtime is synchronous and POSIX/Linux based: terminal
signals can record a request during HTTP inference or blocking shell execution without another
reader thread, hotkey framework, or a full-screen TUI. Shell processes already run in a separate
session. The short GUI helper now does too, so Ctrl+Z cannot suspend an in-flight gesture/capture.
Autonomous terminal echo is disabled; pending premature input is flushed before enabling normal
line editing at a safe boundary. Input reads one canonical line without Python TextIO prefetch.
Original terminal attributes and the prior SIGTSTP handler are restored on every ordinary exit,
exception, EOF and KeyboardInterrupt. SIGKILL cannot run cleanup; a terminal may need `stty sane`
after forcibly killing its controller.

## Exact boundaries

There are gates at the outer Worker-step boundary, immediately before each inference attempt
(including reasoning-loop retries), after inference before admitting reasoning/action history,
and after structural validation/phase gating immediately before **dispatch admission**.

Dispatch admission is the last flag check before appending the accepted action and recording its
audit event. The action's execution, result recording, checkpoint publication/history refresh and
any Worker restoration are then one indivisible runtime operation. A request after this admission
reserves the next boundary; it cannot undo an already admitted action. A pause request is not a
promise that a command ends immediately. Existing command/HTTP timeouts still apply.

**During inference:** the active generation finishes normally. Core pauses before dispatching its
proposal. If input contains a message, that proposal is discarded and a new generation receives
it; the discarded proposal is not represented as an executed action. Empty resume lets the same
proposal proceed through normal validation. A later request during validation is caught by the
final dispatch gate. Discarded generations still count toward the existing max-step ceiling.

**During shell/tool execution:** Core completes the action. For shell it admits output and saves
OP evidence; for `release_worker` it also finishes the normal fresh checkpoint, provider restoration
and cold prefix reconstruction. Only then does it pause, before the next Worker turn. No PAUSE
path kills a shell process, changes process-group timeout cleanup, rolls back /work, or cancels a
checkpoint transaction. Expert and GUI results similarly enter history before the next boundary.

The synchronization is local to Worker generation/dispatch. PAUSED prohibits further Worker work;
it does not freeze independently running GUI applications, shell-launched background jobs, remote
services, or other external writers. Such activity can still change the external world. There is no
new process-tree freeze/rollback system. Only one controller per terminal/state directory is supported.

## Semantic delivery and Worker actions

The submitted message is appended once to ordinary chronological Recent History as:

```
role: user
content: USER MESSAGE AT SAFE BOUNDARY:\n<original user text>
```

Completed results precede this message. The next normal `build_worker_messages` call includes both.
Core does not classify the text, automatically mutate Project State, summarize chat, or synthesize
an acknowledgement. If an inference had already assembled an older context, new input restarts it.
No input is accepted before PAUSED. Resume without a message creates no semantic message.

Two strict action shapes are advertised only in interactive mode:

```json
{"action":"message","text":"I will leave exporter.py unchanged."}
{"action":"wait_for_user","text":"Overwrite the existing file or create a new one?"}
```

`message` displays Worker-authored text and continues autonomously. Both chat actions append a
plain factual `MESSAGE RESULT: user-facing text displayed.` to history as role=user, closing the
assistant action/result pair just like other tools. This is execution evidence, not a synthesized
Worker acknowledgement. `wait_for_user` then enters the shared pause boundary before any further
Worker turn. Empty resume is allowed; Worker
chooses what to do with the missing answer. Both actions are available during initialization,
periodic review and HIGH, but neither unlocks or completes those phases. Normal project and FINISH
validation remain authoritative. Noninteractive Core rejects these actions and schemas omit them.

USER appears in bright cyan and P.A.V.L.U.S.H.A. in bright magenta. Technical reasoning remains dim.
The renderer prints chat text literally, without interpreting Rich/Markdown-looking user content.
Colors, labels and runtime statuses are UI-only; no generated styling is added to model messages,
Project State, checkpoint data, audit data or history archives. Literal user-supplied text is retained
as semantic input, including text that happens to resemble markup. Standard NO_COLOR/TERM behavior
still applies, and speaker labels remain distinguishable without color.

## Checkpoint and recovery behavior

Chat remains Recent History, not a second durable store. Before HIGH/reset or intentional Worker
release the Worker is prompted to preserve durable requirements through existing Project State.
Committed recovery, snapshot validation, OP ledger and /work preservation are unchanged. Normal
HIGH archives chat with the rest of history and clears it once; it is not automatically replayed
from an archive. Cold process restart restores only committed Project State/handoff, never terminal
editing buffers or old chat messages. The original task identity remains required for restart.

If provider context overflow would cold-recover while any accepted user intervention remains in
Recent History, Core stops with an explicit error instead of silently dropping or replaying those
messages. This intentionally conservative policy also applies to previously consumed interventions
still in History; after a successful HIGH history reset ordinary cold overflow recovery is available
again. Raw chat is not promised to survive process loss. Facts required for recovery remain the
Worker's responsibility to materialize before committing a recovery point.

## Verification and review files

`tests/test_interactive.py` has 19 deterministic tests covering autonomy, inference pause/discard,
empty resume, dispatch-time pause, completed shell evidence ordering, composition ownership,
Worker question/reply, terminal FINISH, phase gates/checkpoints, release restoration, GPU shell
admission, distinct literal presentation, terminal restoration, Unicode/canonical EOF, noninteractive
stdin/EOF, explicit overflow failure, helper signal isolation, and actual Ctrl+Z through a controlling
pseudoterminal in a fresh process. They use scripted providers and direct boundaries/pipe acknowledgements,
not arbitrary sleeps. The tests do not exercise a live language model or claim semantic compliance
with an instruction by a model.

Changed files:

- `pavlusha_agent/interactive.py`: bounded terminal control, shared safe input, chat action validation/prompt.
- `pavlusha_agent/runtime.py`: gates, chronological delivery, message/wait dispatch, terminal FINISH and Worker effort passthrough.
- `pavlusha_agent/worker_contract.py`: interactive-only schema variants.
- `pavlusha_agent/cli.py`: explicit `--interactive` and `--reasoning-effort` switches.
- `pavlusha_agent/live.py`: prominent literal speaker presentation/status.
- `pavlusha_agent/gui.py`: isolate the active helper from foreground terminal pause signals.
- `tests/test_interactive.py`: focused tests.
- `tests/test_reasoning_effort.py`: five tests covering omission, arbitrary values, real request
  serialization in both transports, rejection without fallback and request settings after release.
- `README.md`, `docs/interactive-chat.md`: feature usage and boundary/limitation report.

## Configurable Worker reasoning effort

`--reasoning-effort <string>` is optional and has no choices/enum, normalization, model registry,
or capability policy. If absent, no `reasoning_effort` request field is supplied. Otherwise
Core passes the exact string to the existing ChatProvider configuration arguments. Both
`complete_turn` and `complete_turn_stream` already serialize this field in `/v1/chat/completions`;
no new provider/model abstraction was added. HTTP or streamed provider errors remain real runtime
errors, with no removal/retry fallback. Acceptance by a backend does not prove how it interpreted
the effort internally. `--raw-reasoning-limit 20000` remains the existing compatibility no-op.

In this adapter effort is an inference-request option, not a native model-load setting. Every
Worker generation/retry receives it from the same invocation arguments, including the next turn
after `release_worker` has restored a new instance ID. Existing supported loaded-instance settings
(such as `reasoning_budget_message`) still use the same unload/restore path; they are neither
reinterpreted nor overwritten by this request knob. There is no second configuration store.
The release wire test verifies both exact request effort before/after reload and preservation of
that existing supported load setting. It does not claim real GPU unloading behavior.

The continuation-only `StateStore.mark_running` helper introduced in the preliminary chat patch
was removed. StateStore is now unchanged from the repository baseline; State schema, checkpoint
format, TASK identity and recovery protocol were not modified for effort/terminal FINISH.

## Current validation

| Check | Result |
| --- | --- |
| `.venv/bin/python -m unittest tests.test_reasoning_effort tests.test_interactive tests.test_worker_release` | 43 tests, OK; 2.097 s |
| `PAVLUSHA_LIVE_NETWORK=1 .venv/bin/python -m unittest discover -s tests` (approved unrestricted run) | 290 tests, OK, 4 opt-in real-browser skips; 11.240 s |
| `.venv/bin/python -m compileall -q agent.py bench.py pavlusha_agent tests tools` | Exit 0 |
| Import of agent, CLI, runtime and provider | Exit 0 |
| `.venv/bin/python agent.py --help` | Exit 0; both interactive/effort switches advertised |
| `git diff --check` | Clean |

The four skipped tests require `PAVLUSHA_TEST_GUI_BROWSER=1`: one browser integration and three
browser-lifetime cases. The full run includes existing atomic/cold recovery, shell timeout/process
group cleanup, GUI display/helper, Expert, Worker release and GPU shell tests. Existing expected
negative-CLI diagnostics and mocked-HTTP ResourceWarnings are not test failures.

The earlier multi-task chat probe exposed the missing message-result evidence issue and led to
its existing fix. Its FINISH continuation behavior is intentionally superseded by terminal FINISH;
those earlier traces are historical, not the current runtime contract. The current regression test
queues a next-task message and another Worker action after FINISH and verifies that neither is read
or executed, no paused prompt appears, status is finished and the existing checkpoint is retained.
/quit and EOF remain tested at an active `wait_for_user` boundary.

No commits, tags, releases or pushes are part of this change.


## Current real model dogfood: one TASK, terminal FINISH

The already loaded `qwen/qwen3.8-27b` on LM Studio at `127.0.0.1:1234` was used through the real
provider, real bwrap and a controlling pseudoterminal. A small temporary observer saved actual
outgoing chat request bodies and then called the unchanged real HTTP transport; no model answers
or runtime behavior were mocked. All work/state files were isolated outside the repository.

The interactive invocation omitted effort, preserving the loaded model's default. Its 10 actual
request bodies all omitted `reasoning_effort`. The model emitted `message`, entered `wait_for_user`,
received a filename, then executed a bounded two-second shell operation. Ctrl+Z was sent after
`phase.txt` contained only `started`. At PAUSED the command had added `completed` and OP0001 had
been recorded. No new request was issued during a deliberate composition interval. The actual
next request included completed OP0001 evidence before the user's intervention, which prohibited
`forbidden.txt` and substituted `allowed.txt` containing INTERVENTION_OK. Qwen acknowledged via
`message`, recorded its own plan deviation, created/verified the permitted file and completed work.

Accepted FINISH printed `atomic dogfood done` and the process exited **0 at 109.72 seconds**. There
was no input prompt after FINISH, no `/quit` was needed, and the ledger contained exactly one
finished event and no interactive continuation. Final run status was finished; forbidden.txt never
appeared. The same original TASK identity and unchanged State/checkpoint format were retained.

A separate noninteractive invocation passed `--reasoning-effort low`. All **four** observed
`/v1/chat/completions` requests carried that exact string, the server returned successful real
answers, the Worker created/verified effort.txt containing EFFORT_OK, and FINISH terminated with
exit 0. Both runs finished in **129.22 seconds** combined. Backend acceptance and request forwarding
were verified; internal interpretation of effort and quantitative reasoning behavior were not.
No real unload/reload was performed in this dogfood: preservation across release/new instance ID
and existing model-load configuration was verified by the focused tests using scripted native API
responses and real chat request serialization. Unknown-value rejection is similarly a transport
error regression test, not a claim that this server rejects every unknown string.

Artifacts: `/tmp/pavlusha-v03-atomic-dogfood-pavmvm13/` contains clean/raw interactive terminal
transcripts, `interactive-requests.jsonl`, `effort-requests.jsonl`, the second terminal log,
`events.json`, and both task directories. The preliminary probe failed in the temporary observer's
import setup before starting the runtime; correcting its import path required no repository patch.
