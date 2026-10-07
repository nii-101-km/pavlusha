# Worker action generation and validation

Worker responses keep the existing single JSON action protocol. Every runtime
Worker inference request sends LM Studio's OpenAI-compatible
`response_format.type = "json_schema"`, with `json_schema.name = "worker_action"`,
`strict = true`, and a schema built by `pavlusha_agent/worker_contract.py`.
The existing provider sends this parameter in both ordinary and streaming chat
completions. Expert and legacy State Manager requests are unchanged. There is no
tool-calling migration or unconstrained fallback when the provider rejects a schema.

## Active phases

Schema selection follows the existing runtime gates in their priority order:

| Phase | Worker action variants |
| --- | --- |
| Project State uninitialized | `project_init` |
| Required history checkpoint | `project_update`, `project_review_complete` |
| Periodic Project State review | `project_update`, `project_review_skip` |
| Ordinary execution | `shell`, `finish`, `project_update`; GUI actions by default when dependencies are available (`--no-gui` disables); `ask_expert` when Expert is enabled; `call_function` variants when `--functions` is supplied |

GUI variants are `gui_start`, `view_gui`, `click`, `right_click`, `drag`,
`type_text`, `press_key`, `hold_key`, and `gui_close`. Legacy `drop_context` and `compact_context` are not
enabled by the active runtime and are not advertised in generation schemas.
`project_init` is not available after initialization; review-only replies are not
available during ordinary execution.

Custom function variants fix both `action` and the registered `name`, and derive
the `arguments` schema from the Python declaration. Core validates again before
execution; call contract errors become normal factual observations. See
[custom functions](custom-functions.md) for types, trust and recovery semantics.

Each action has its own object branch with a fixed `action`, its required payload
fields and no additional properties. Project updates similarly have separate
`op` branches for `add_design`, `supersede_design`, `add_work`, `update_work`, and
`record_deviation`. This prevents optional fields from making another action's
payload schema-valid. Optional fields remain optional: shell `network`, `gpu`,
`release_worker`, and `timeout` retain their existing defaults and independent
capability semantics. Status values in generated replies use canonical uppercase.

## Three boundaries

1. **Native generation:** LM Studio uses the supplied schema to constrain final
   action generation. The local Qwen streaming verification produced complete,
   schema-valid JSON in each successful turn, with reasoning separately returned
   as `reasoning_content`. Backend schema support does not establish that every
   JSON Schema keyword or Core rule is enforced during generation. A token ceiling,
   interruption, or provider failure can still prevent a complete response.
2. **JSON parser:** the existing parser requires a decodable outer JSON object.
   It never rescues a nested mutation from a malformed outer object. Runtime
   additionally refuses any action returned with `finish_reason = "length"`,
   including an otherwise complete JSON object. No partial stream is executed.
3. **Core:** existing action and Project State validators remain authoritative
   for payload types, nonblank text, IDs/references, state transitions, ACTIVE
   work invariants, UTF-8 handoff limits, evidence structure, phase gates, network
   permissions, timeout clamping, Worker release and execution. Finish still
   requires completed work. Generation constraints do not replace these checks.

Malformed or unsupported replies still enter the existing bounded invalid-action
retry path. Empty reasoning-only responses retain the existing empty-action
recovery boundary. Provider/schema rejection stops without retrying unconstrained.
Interrupted reasoning-loop generations retain their existing discard/retry behavior
and the same phase schema on retry. Checkpoints, prefix/history recovery, and shell
execution are unchanged.

## Verification

Install `requirements-test.txt` for the independent JSON Schema reference validator.
`tests/test_worker_structured_output.py` checks phase selection, complete action
families, required payload fields, provider request serialization in both transports,
periodic review and return, malformed/unexpected responses, provider rejection,
and truncated completions without executing partial actions. Existing lifecycle,
reasoning-loop, release, GPU, network and checkpoint suites remain in place.

The local real Worker probe exercised initialization, ordinary shell, periodic
update, a bounded shell with all three optional capabilities enabled, restoration,
another periodic update, and finish through the same CLI/provider/runtime path as
`agent.py`. All six Worker requests received by LM Studio contained the intended
schema; all six completed responses were schema-valid and accepted by Core.
The command verified NVIDIA compute device visibility; it did not run OCR or
inspect unrelated GPU processes. Reasoning and streaming were preserved.

A separate real truncation probe used identical messages and schema for two
requests in the ordinary phase. The control returned a complete `finish` action
(30 reasoning tokens, 42 completion tokens), accepted by Core. A ceiling of 36
tokens returned the incomplete `{"action":"finish` with `finish_reason = "length"`.
Core left the run unfinished, recorded no summary and executed no shell operation.
Reasoning remained enabled and separately returned in both requests. Automated
tests also verify that even complete-looking JSON with a length finish reason is
not executable, and that three invalid replies retain the existing failure limit.

Measured time to first streamed reasoning token was 1.04–1.92 seconds before
release, 4.66 seconds on the first request after model reload, and 1.81 seconds
on the following request. Final action content began after 1.91–5.35 seconds;
complete inference requests took 4.02–7.46 seconds. Backend logs showed prefix
reuse across ordinary/review schema changes and fresh prompt evaluation after
the intentional model reload. These are observations from one bounded local run,
not a controlled performance comparison with unconstrained generation.
