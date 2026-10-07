# Lexical recovery exhaustion: controlled CLI dogfood

Local artifact paths below are generalized as `/path/to/dogfood`; the artifacts are not shipped in this repository.

On 2026-10-06 the public `agent.py` path was exercised with a local HTTP SSE
replay server and a real PTY. The reasoning stream was the saved, unmodified
3953-word Qwen laboratory trace from
`tests/fixtures/qwen_lab5_step10_reasoning.txt`. This is a controlled replay,
not a newly generated model attractor or a live-model understanding test.
No runtime/provider patch or detector override was used.

The launch retained ordinary defaults, including interactive, live, network,
GUI, Project Map and `recover`; only transport/model, directories, explicit
context capacity and `--max-reasoning-loop-recoveries 1` were selected.
The full argv is saved in each run's `command.json`.

Artifacts on the test host:
`/path/to/dogfood/human-escalation-smoke-20261006-203558/`.
Each run contains `terminal.log`, `requests.json`, `command.json`, `state/`
and `work/result.txt`; `summary.json` records assertions for both modes.

- Interactive PID 145578: initialized the project, created `result.txt` with
  value 42 through real bubblewrap shell, detected the trace at word 880,
  retried once, detected again at word 880, then entered `NEED USER`.
  State at the wait was identical as parsed JSON to State before the
  interrupted generations; the committed checkpoint and file were preserved.
  Empty Enter caused no new inference. Nonblank guidance appeared in the next
  HTTP request as `USER MESSAGE AT SAFE BOUNDARY`. The same process ran a real
  shell verification, recorded both work items DONE and reached FINISH, exit 0.
  Seven requests, two executed shell operations; interrupted actions were absent
  from subsequent History and were never executed.
- Explicit `--no-interactive`, PID 145599: the same two detections and one retry
  ended with exit 2 and a bounded error explaining that human escalation is
  unavailable. Four requests, no input wait, unchanged State/checkpoint/work.

The scoped regression suite is `tests/test_human_escalation.py` alongside the
existing reasoning-loop, interactive and atomic-checkpoint recovery suites.
It also covers the default ceiling of three retries, repeated exhaustion after
human reset, whitespace input, /quit/EOF, integrity/provider failures, cold
restart after quitting at the wait, and escalation before project initialization.
The detector, retry instruction, State schema and checkpoint format are unchanged.
