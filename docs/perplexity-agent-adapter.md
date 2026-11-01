# Experimental Perplexity Agent model adapter

The adapter implements the existing model backend interface without changing the
kernel. Select `--backend perplexity-agent`, `--base-url https://api.perplexity.ai/v1`
and a model such as `openai/gpt-6.1-terra`. Set `PAVLUSHA_API_KEY` in the
environment, or select another variable with `--api-key-env`. Do not put the key
in the task, command-line arguments, session files, or committed configuration.

Perplexity Agent requires a top-level object JSON schema. When the kernel offers
multiple action branches, the adapter sends a union of their fields and removes
only null fields from other actions. The kernel still validates the response
against the original action schema before invocation. Conflicting definitions of
the same field are rejected explicitly rather than silently changed.

The adapter supports text and embedded PNG/JPEG observations. When a streaming callback or cancellation check is
provided, it reads Agent API SSE output incrementally and accepts a response
only after `response.completed`. Cancellation is checked before the request,
between received SSE lines and after completion; while waiting for a network
read with no events it may be delayed until the next event or network timeout.
Closing the stream stops local consumption but does not prove the remote service
stopped generation or billing. The adapter rejects incomplete responses and does
not execute native provider tool calls. Model release and
context-size discovery are unavailable; set the context budget explicitly.
The latest usage details, including cached input tokens when reported, are
available as `provider.last_usage`; they are not yet displayed or persisted by
the kernel. Perplexity server-side caching is not guaranteed by this adapter.

## Cache investigation, 2026-10-29

Live requests used the Agent API, `openai/gpt-6.1-terra`, synthetic text and
the usage/cost fields returned by Perplexity. With a fixed experimental
`prompt_cache_key`, two identical 6,335-token requests wrote 6,332 tokens and
then read 6,332 cached tokens. Without the key, both identical requests read
zero cached tokens. However, appending an assistant message and a new user
message caused another full cache write and zero cached reads. The same occurred
using `previous_response_id`. Repeating that identical continuation then read
its cache. Explicit `prompt_cache_options` and input breakpoints were also
accepted but did not yield a read after appending a turn; the response echoed
the options as null. These small probes do not establish service guarantees.

The response reported a cache write cost of $0.00792 for 6,332 tokens and a
read cost of $0.00032 for the identical repeated request, excluding output.
Thus cache writes can be billable and cannot be treated as ordinary input or
zero cost. `last_usage` exposes reported cache reads, writes, total cost and
currency where supplied; it does not infer missing metrics.

The adapter now sends a randomly generated stable `prompt_cache_key` per adapter
instance; a restarted session gets a new key. Explicit breakpoints are not used.
Follow-up probes found that long, stable `instructions` can be reused when user
input changes: with a cache key, the next request read 3,159 of 3,188 input
tokens. With a JSON schema and a growing conversation, the next request read
3,193 of 3,248. A real two-turn kernel run using the adapter and a simulated
shell read 1,660 of 1,930 input tokens on the second turn; the first and second
requests reported total costs of $0.00256 and $0.00060. The stable system
instructions are preserved and user messages retain their roles. These results
demonstrate reuse in this scenario, not a guarantee that all history, changing
action schemas, or resumed sessions will be cached. The cache key is an
experimental parameter verified by live requests, not a documented guarantee.

The experimental `examples/arishka-pavlusha.config.json` combines this adapter
with the orchestration module to delegate to a separately running local
llama.cpp agent. Configure the local server and child profile before use. The
adapter itself does not start the local server.
