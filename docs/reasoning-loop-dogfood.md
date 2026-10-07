# Qwen laboratory reasoning-loop investigation

Local artifact paths below are generalized as `/path/to/dogfood`; the artifacts are not shipped in this repository.

## Source and configuration

The full terminal transcript supplied as
`/path/to/dogfood/state/xlsx-lab5-dogfood/state2.log` contains
the latest launch command at line 5151 and the interrupted STEP 10 reasoning
at line 5286. The same reasoning is captured in `state1.log`.

The command uses both Office packs, `--max-steps -1`, `--max-tokens 8000`,
`--output-limit 12000`, `--reasoning-effort medium`, and `--interactive`.
It does **not** specify `--reasoning-loop-recovery`. Parsing that command
with the CLI at the time of the run gives `reasoning_loop_recovery='off'` and
`max_loop_retries=3`. The reported model is `qwen/qwen3.8-27b`.

In `off` mode `_worker_generation` does not instantiate a detector.
Interactive mode and reasoning effort do not enable it. Consequently the
absence of loop interruption in this run is expected, not a detector false
negative. Following this investigation, the production default was changed
from `off` to `recover`, equally for interactive and non-interactive runs.
Explicit `off` still disables detection; `observe` records detection but
allows generation to continue. This default change does not alter the
detector or recovery path described below.

## Replay of the actual captured generation

`tests/fixtures/qwen_lab5_step10_reasoning.txt` preserves the full reasoning
between the STEP 10 header and `Interrupted.`, including the unique preamble
and final partial sentence. Only terminal framing and trailing whitespace
were removed, with a final newline added. No repetitions were synthesized.
The fixture is also a substring of the full `state2.log` transcript.

It contains 3,953 Unicode words, 24,438 characters before the final newline,
and 62 occurrences of the repeated “I'm noticing the logical operators are
getting lost” opening. Fixture SHA-256:
`c5fe71d72cbb891d9995ed12e27c7365129d05e72c8a718cf8791bae17b1ecfa`.

The unchanged detector compares sets of four-word shingles in 240-word
windows every 40 words. Eligible windows must start at least 480 words
apart; two consecutive scores of at least 0.70 confirm a loop.
Word ranges below are zero-based and half-open.

| Words consumed | Current window | Best older window | Intersection / union | Jaccard | Consecutive matches |
| --- | --- | --- | --- | --- | --- |
| 720 | [480, 720) | [0, 240) | 62 / 176 | 0.352273 | 0 |
| 760 | [520, 760) | [40, 280) | 62 / 136 | 0.455882 | 0 |
| 800 | [560, 800) | [80, 320) | 62 / 97 | 0.639175 | 0 |
| 840 | [600, 840) | [120, 360) | 62 / 62 | 1.000000 | 1 |
| 880 | [640, 880) | [120, 360) | 62 / 62 | 1.000000 | 2: confirmed |

Before word 720, no older window satisfies the minimum lag; the reported
zero score represents no eligible comparison. The preamble dilutes the
first three eligible comparisons. At word 840 the first match satisfies
the threshold, but confirmation requires the second match at word 880.
This is approximately 22.3% of the captured generation. Splitting the same
trace into deltas of 1, 7, 97, 511 characters or one complete delta gives
the same confirmation.

There is no algorithm change, so before/after detector measurements are
identical. Existing streaming transport/runtime tests now replay this
actual trace in all three modes: `off` produces no event; `observe` reports
word 880 without interruption; `recover` interrupts, rejects the action
from that generation, and retries through the existing recovery path.
These tests do not call a model.

The user-quoted 62-word excerpt is a separate supplemental fixture with
explicitly reconstructed repetition counts. Eleven repetitions cannot
reach the lag requirement, twelve produce one match, and thirteen confirm
at word 760. It is not substituted for the actual captured generation.

For a negative control, the existing real GLM-OCR turn 38 (7,825 words)
plus one laboratory excerpt remains undetected despite extensive reuse of
topic and code vocabulary. Its maximum Jaccard score is 0.270349.
The existing detector and recovery cases retain their expected outcomes.

## Initial investigation validation and scope

- `.venv/bin/python -m unittest tests.test_reasoning_loop -q`: 21 tests, OK.
- `.venv/bin/python -m unittest discover -s tests -q`: 370 tests, OK;
  26 skipped.
- The initial investigation added actual/supplemental fixtures and regression
  tests without changing production code or thresholds. The subsequent
  default change modifies only the CLI default/help, documentation and tests;
  a default-mode runtime replay confirms the same word-880 interruption.
- No changes to Core authority, State, checkpoints, recovery semantics,
  interactive/provider policy, or DOCX/XLSX packs. No escalation policy
  was introduced and no new LLM generation was required.

The workspace already contained changes from earlier Office/custom-function
work. Those are outside this investigation. No changes were committed.
