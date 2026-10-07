# Custom call-functions

`--functions /absolute/path/my_functions.py` enables a trusted local Python
module for a run. The option is repeatable: independently loaded modules export
their functions into one registry, in launch order. Exported names must be
globally unique; duplicates fail at startup with the name and both source paths.
Every module must load and validate successfully before Worker starts; there is
no partial registry. With the flag omitted, Worker prompts, action schemas and
execution retain their previous behavior. Shell remains available. This is a
small external capability boundary, not a plugin manager or another agent system.

```python
# my_functions.py
def multiply(a: int, b: int = 2) -> dict:
    """Multiply two integers."""
    return {"result": a * b}
```

```sh
python agent.py --functions /absolute/path/my_functions.py \
  "Use multiply with a=6 and b=7, then report the result."
```

For tasks requiring several capability packs:

```sh
python agent.py \
  --functions /absolute/path/docx_functions.py \
  --functions /absolute/path/xlsx_functions.py \
  "Update the spreadsheet and use the resulting figures in the Word report."
```

These are user-supplied modules, not built-in DOCX/XLSX packs. Functions keep the
same flat `call_function` action; choose unique names such as `docx_read` and
`xlsx_read`. Module ordering never grants override priority.

Worker sees function names, docstrings, argument schemas, required arguments,
defaults and the declared result schema. It requests one ordinary JSON action:

```json
{"action":"call_function","name":"multiply","arguments":{"a":6,"b":7}}
```

Core validates the registered name and arguments, executes the function, records
an ordinary `OP` ledger entry, and returns a `FUNCTION RESULT (OPxxxx)` observation
in Recent History, for example `{"name":"multiply","result":{"result":42}}`.
Functions never receive a Core, StateStore, Project Map or Worker reference.
Project State initialization, checkpoint/review admission, FINISH and safe
interactive boundaries remain authoritative. Calls do not release/reload Worker,
change reasoning configuration, or manage the provider/GUI lifecycle.

## Export and type contract

Only public Python functions defined in that module are exported; imported
functions, classes and `_private` helpers are ignored. Aliases, async/generator
functions, positional-only parameters, `*args` and `**kwargs` are rejected at
startup. Names are Python identifiers of at most 128 characters. Every argument
needs an annotation. Ordinary and keyword-only parameters are accepted; a Python
default makes an argument optional. Defaults must match the annotated JSON type.

Supported annotations are `str`, `int`, `float`, `bool`, `None`, `Any`, `list[T]`,
`dict[str, T]`, unions (`T | U`, `Union`, `Optional`) and bare `list`/`dict`.
`Any` and bare containers accept JSON values only. Future/string annotations are
resolved using Python's type-hint resolver. Other declarations fail clearly on
startup. An optional return annotation describes and validates the result;
without it, the result must still be JSON-compatible. Docstrings are descriptions,
not a separate schema language. Keep them compact.

JSON-compatible means null, bool, finite number, string, list and dict with string
keys. Booleans are not accepted as integers; integer inputs are accepted for
`float`. Tuples, sets, bytes, custom objects, non-finite numbers and cyclic
containers are contract errors, never silently converted with `repr()`.

Unknown functions, invalid arguments, ordinary function exceptions (including
`SystemExit`) and invalid results are observable errors with `error` values
`unknown_function`, `invalid_arguments`, `function_exception` and `invalid_result`.
An extra field such as `release_worker` or `timeout` is an argument error.
Invalid JSON envelopes still use the existing invalid-action retry mechanism.
Without `--functions`, a `call_function` action is unavailable.

Serialized result values above `--output-limit` are withheld wholesale and return
`output_limit_exceeded`; errors have bounded detail. The ledger stores the normal
small excerpt, not an entire large return value. Recent History uses the existing
complete-step archive/reset mechanism. Calls count as operations for periodic
Project State review; this does not grant functions authority to modify State.
For large data, return a compact job ID, artifact path or narrower structured fact.
A function's console prints are not its result protocol.

## Trust, execution and recovery

**Every Python module is trusted user code running with the controller process's
permissions, environment and host filesystem/network access. It is not sandboxed.**
`--network` controls the existing shell/GUI permission; it does not sandbox Python
functions. Importing each module executes its top-level code once before Worker
starts. The registered function set is fixed at launch; Worker cannot choose
another module or arbitrary Python symbol through this action. Put the module
outside the Worker-writable work directory and review it before each launch.
Startup failure prevents Worker execution but cannot undo trusted import-time
side effects already performed by earlier modules.

Calls run synchronously in the main process. There is no function timeout or safe
cancellation mechanism. `--command-timeout` still applies only to shell/GUI.
Functions should implement bounded API/service timeouts themselves. A hung call
can hold the runtime indefinitely; process exits, memory exhaustion and malicious
code cannot be contained by catching exceptions. A subprocess executor could
provide additional crash/timeout containment but is outside this minimal version.

Ctrl+Z requests the existing safe pause. A started call completes, its result is
recorded and appended to history, then interactive input begins at the next
boundary. A request before dispatch uses the existing held-proposal behavior.
No second pause system is introduced.

Checkpoints do not serialize functions, Python objects or module state. Recovery
loads all modules again from the user's launch configuration (repeat the same
flags with unchanged trusted files to keep the same capabilities). Restart
without any `--functions` flag disables functions even if older history contains a call. Completed factual
ledger entries survive normally; Recent History is not restored as a live Python
session. The checkpoint remains the existing committed recovery source.

There is **no exactly-once guarantee**. If an external side effect completes and
the process crashes before `record_operation` publishes its record, recovery has
no durable result for that call. Core does not automatically replay actions, but
a subsequent Worker decision can call it again. Even a recorded result may be
outside the latest committed checkpoint's working context. Use service-level
idempotency keys or inspect the external service when repetition matters.

## Implementation scope and inspection

The extension uses `functions.py` for loading, discovery, schemas and strict
validation; `cli.py` for the opt-in flag; `worker_contract.py` for phase-gated
schema branches; `runtime.py` for dispatch and ledger/history admission; and
`live.py` for the action label. It reuses `StateStore.record_operation` through
its existing command/stdout/error fields (`command = call_function NAME`).
No State/checkpoint schema or new recovery format was needed.

Inspection covered the Worker parser in `core.py`, shell validator/executor and
output admission in `sandbox.py`, ordinary and release dispatch in `runtime.py`,
complete-step Recent History in `working_context.py`, Project State gates,
StateStore's factual ledger, atomic checkpoint/recovery, interactive boundaries,
provider release lifecycle, CLI and existing lifecycle tests. The dispatch branch
sits after the shared admission boundary and returns before shell/release logic.
The shell subprocess timeout cannot safely cancel an in-process Python call.

`tests/test_functions.py` covers declarations, schemas, execution/errors, bounded
results, ordinary history/ledger, disabled behavior, FINISH/shell continuity,
checkpoint gating, cold recovery, side-effect/recording failure and real SIGTSTP
pause during a function call.

Single-module verification: the full suite ran 303 tests successfully (6 skipped),
including 13 targeted custom-function tests. Compile/import sanity and
`git diff --check` passed. A real local Qwen Worker completed four turns through
the CLI: `project_init`, `call_function multiply(6, 7)`, `project_update`, `finish`.
The function returned `42`, the ledger recorded `OP0001`, both work items became
DONE using that observation, and the run finished without shell execution or
Worker release. These are observations from one bounded local smoke run.

Repeatable-module verification: `tests/test_functions_modules.py` adds six tests
for CLI compatibility, ordered schemas/discovery, duplicate and invalid-module
startup failures, shared result/error dispatch, and cold recovery without replay.
The full suite ran 309 tests successfully (6 skipped); compile/import sanity and
`git diff --check` passed. A real local Qwen Worker completed one task using two
separate modules: `sum_terms(8, 5)` returned `13` as `OP0001`, then
`scale_total(13, 6)` returned `78` as `OP0002`. The run completed through
`project_init`, two `call_function` actions, `project_update` and `finish` without
shell execution. No DOCX/XLSX packs are included in this change.
