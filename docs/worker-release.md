# Worker release for bounded shell execution

An ordinary shell action may include an optional boolean `release_worker`:

```json
{"action":"shell","command":"python workload.py","network":false,"timeout":120,"release_worker":true}
```

The command remains opaque to Core. Omission or `false` preserves normal execution
and history. The existing initialization, HIGH and periodic review gates apply.
Network denial does not release the Worker or execute the command. The Worker chooses
its existing integer `timeout` before release; Core clamps it to 1 through the
`--command-timeout` ceiling. Timeout uses the existing process-group TERM/KILL path.
There is no separate shell timeout subsystem.

Before requesting release, materialize durable changes with `project_update`. Core
commits a fresh recovery generation using the existing atomic `complete_project_review`
transaction. This intentional cold boundary replaces the previous handoff with empty text;
it does not summarize reasoning or introduce a checkpoint schema or recovery subsystem.
If publication fails, the backend is not released and the command is not run.

Core runs the same sandboxed command and admits stdout/stderr through the same output
limit. Nonzero exit and timeout remain ordinary results. Launch errors and execution
exceptions become factual results in this mode. Core records the result in the ordinary
operation ledger before restoration. Restoration is attempted in `finally`, including
when execution, result admission or ledger recording fails.

After successful restoration, Core uses existing strict cold restart: committed State,
current filesystem, preserved operation ledger, refreshed Map, and archived previous
History. The selected action and its normal result are supplied to the resumed Worker.
Files are never rolled back. Restoration failure stops the run explicitly without
retrying the command; completed ledger records survive restart. Process death and
failure to persist a result have the same limitations as ordinary shell execution;
this mechanism does not promise exactly-once execution or restoration after process death.

## Backend contract and limits

The adapter uses LM Studio's native REST API, separate from OpenAI-compatible chat.
Providers without that API cannot use this mode. Requests use existing authentication
and a finite positive provider API timeout, with no retries.

The adapter requires exactly one loaded LLM matching the Worker model key or instance ID.
It unloads only that instance and verifies its absence before executing shell. It restores
the same key, passing supported load configuration on a best-effort basis. It confirms
that the returned instance ID is loaded under the same model key and uses that ID for
resumed inference. Exact configuration equivalence and a configuration echo are not
required. No inference/KV state is retained.
A lost unload response still enters restoration. An instance that remained loaded is reused
only if its model key and instance identity are unchanged.

The native load request schema in LM Studio 0.4.25 explicitly supports `context_length`,
`eval_batch_size`, `physical_batch_size`, `parallel`, `flash_attention`, `context_checkpoints`,
`reasoning_budget_message`, `num_experts`, `offload_kv_cache_to_gpu`, `prompt_template`,
`ttl_seconds`, and the six `speculative_draft_*` fields: `mtp`, `simple`, `model`,
`max_tokens`, `min_tokens`, and `min_continue_probability`. Present supported values
are passed back, including `physical_batch_size=512` and `speculative_draft_mtp=true`.
Unsupported or non-restorable loaded-instance fields are omitted and do not block release
or restoration; LM Studio uses its defaults for them. Configuration/performance settings
may differ after reload. The required invariant is the same Worker model key successfully
loaded for resumed inference, not bit-for-bit deployment/performance equality.
See LM Studio's [model list](https://lmstudio.ai/docs/developer/rest/list),
[load](https://lmstudio.ai/docs/developer/rest/load) and
[unload](https://lmstudio.ai/docs/developer/rest/unload) contracts.
Instance aliases may change on reload. Deployment settings not exposed by the API are
outside this adapter's guarantees.

Restoration requires a functioning backend. A server outage can prevent successful reload;
this stops runtime rather than claiming recovery. `release_worker=true` requires exclusive
use of the Worker instance during the release interval. Concurrent external clients or
configuration changes are unsupported. The adapter does not enforce exclusivity, coordinate
external clients or unload unrelated instances.

API-confirmed unload is not proof of VRAM release or exclusive resources. The shell
sandbox is unchanged by release alone: the independent shell option `gpu=true` grants
NVIDIA compute device access (see [GPU shell access](gpu-shell.md)). Real GPU/OCR
verification must independently establish device access, allocation changes, CUDA execution
and resumed inference. Mocked lifecycle tests establish none of those facts.
