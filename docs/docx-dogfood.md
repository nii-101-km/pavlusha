# DOCX pack dogfood report

Local artifact paths below are generalized as `/path/to/dogfood`; the artifacts are not shipped in this repository.

The optional pack was exercised through the existing `--functions` and GUI
mechanisms. No Pavlusha runtime, registry, CLI, State/checkpoint, shell/GUI,
provider, reasoning or FINISH code was changed for the DOCX pack.

This historical run used the initial pack's separate root setting. The pack now
uses runtime `--workdir` automatically; the metrics below belong to that earlier
run. Workdir-only CLI launch and confinement have separate regression coverage in
`tests/test_docx_pack.py`.

## Task and fixture

A four-page Russian launch brief contained headings, formatted text, an image,
a budget table, an implementation table and shared header/footer. The launch
date appeared in body text (split across two identically bold runs), the plan
and the header. Two occurrences of 15 November described historical events and
had to remain unchanged. Budget rows were 120 000, 80 000, 50 000; the original
total was 240 000. The customer's letter specified 22 November.

The real local `qwen/qwen3.8-27b` Worker received the business task, the four
function schemas and instructions for the existing GUI viewer. It was not given
node IDs, the correct budget total, or a replacement plan. Fixture and viewer
were prepared by the harness; the Worker wrote no temporary document glue code.
All input/output paths were under the configured host work directory; the pack
itself remained outside that directory.

## Observed run

Worker chose inspection, bounded reading, targeted reading, one four-edit batch,
rereading with the output revision, rendering, and GUI page review. It independently
computed 250 000 and selected three launch-date occurrences. One cursor request
was rejected as not belonging to the revision/query; Worker recovered using
explicit node IDs. There were no mistaken edits or run-formatting errors.

| Measurement | Observed value |
| --- | --- |
| Worker turns / actions | 16 / 16 |
| Custom-function calls | 7 |
| Shell actions / Worker-written glue code | 0 / 0 |
| GUI actions | gui_start, three page-selection clicks, gui_close |
| Domain errors | one invalid_cursor, followed by recovery |
| Elapsed runtime | 189.4 seconds |
| Prompt tokens per turn | 3 436 initially; maximum 21 016 |
| Sum of reported prompt tokens over turns | 221 990, including repeated context; not unique context size |
| Sum of completion tokens | 4 657 |
| Rendered pages | 4 |

The four edits changed the launch dates to 22 November and the budget total to
250 000. Historical dates, individual budget rows and the image were preserved.
Independent checks confirmed a reread with the new SHA-256 revision, bold-run
preservation and byte-identical untouched ZIP member payloads. Only
`word/document.xml` and `word/header1.xml` changed. Worker reached FINISH normally.

The existing GUI sent an initial page observation and new observations after
three page-selection clicks. Worker reported reviewing all four pages. A
separate QA pass opened each full-resolution rendered PNG; the text, tables,
image and header/footer showed no clipping or overlapping content. The
renderer manifest itself correctly retained `visual_verification=not_performed`;
visual conclusions came from image review, not from the function.

Local evidence for this run is under `/tmp/pavlusha-docx-dogfood/pack`: the
original/corrected documents and render manifest are in `work/`, the append-only
live log is `live.txt`, and ordinary controller telemetry/ledger are in `state/`.
These local artifacts are not required dependencies of the pack or tests.

## Comparison and limitations

No equivalent shell baseline was run. The existing bubblewrap shell sees neither
the host bundled document Python nor the bundled LibreOffice installation under
`/home`; system Python lacks python-docx/lxml/pdf2image and system soffice is
absent. Staging a separate document environment into `/work` or installing one
would materially expand this task. Consequently the measurements above describe
one pack run, not proof that it outperforms a comparably provisioned shell workflow.

The broad initial read returned verbose run/cell metadata and grew context.
Targeted reads/searches are preferable when the Worker already knows what it
needs. The invalid-cursor event is an observed usability limitation of this run;
its precise cause was not established from the retained action telemetry.

Editing remains limited to exact single-line replacements in ordinary paragraphs.
Different explicit formatting, fields, hyperlinks, anchors, revisions, drawings
and content controls are refused. Reading does not resolve fields or provide all
Word constructs; coverage warnings are part of the contract. LibreOffice is not
a guarantee of Word-equivalent layout. Rendering requires explicitly configured
dependencies and Linux no-overwrite directory publication. The root checks reject
ordinary traversal/symlink escapes but are not a sandbox or protection against
malicious concurrent filesystem races. No exactly-once or automatic retry
semantics were introduced.

## OMML reading regression (laboratory 5)

The real `Лабораторная работа 5.docx` was inspected and read through the pack
with the ordinary workdir pointing to `work/xlsx-lab5-dogfood`.
All 27 OMML equations are represented, with no omissions. Twenty-six are
complete under the supported reader. The remaining equation is an empty
square-root example in paragraph `word/document.xml:p87`; its missing argument
is explicitly represented and reported as `missing_or_empty:rad/e`.

Variant 8 is in table 1, physical row 9, cell 2,
`word/document.xml:p55`. Literal search through `docx_read` returns
`y=sqrt(3(x)^(2)−9x+6)`, with `math_partial=false` and `editable=false`.
The regression fixture `tests/fixtures/docx_omml_variant8.xml` is the original
equation subtree from that cell. Source SHA-256 stayed
`641779299f85b20be26fb1b9b23feaaaa9917ee478d0bb8cc75f1009d81452b5`.
This check verifies reading, not formula evaluation or visual approval.

The same real-file check also ran through `FunctionRegistry` with both
`--functions tools/docx_functions.py` and `--functions tools/xlsx_functions.py`:
all eight functions registered and the DOCX calls returned bounded JSON results.

Final validation: the project `.venv` full suite passed **359 tests**, with
25 explicit skips for optional dependencies/features. All **29 DOCX tests**
passed separately with the bundled document Python and real LibreOffice rendering.
The six new tests cover the real variant-8 subtree, display math, unsupported
and empty constructs, long-formula pagination, excluded text boxes, and preserving
equations when editing ordinary text. The additional full-suite attempt in the
document Python could not pass because that environment lacks controller
dependencies (`jsonschema`, `rich`, Tree-sitter, Xlib); no dependencies were
installed and runtime files were not changed for this fix.

## Transport budget and context footprint regression

The restored laboratory-5 log records `OP0007` and `OP0008` returning
`output_limit_exceeded` with `output_limit_chars=12000`. The pack measured compact
JSON, whereas registry admission serializes with spaces. The correction is local
to DOCX read: full spaced JSON, including the function envelope, must fit
`min(max_chars, CLI --output-limit)`. Existing cursors carry overflow into the next
call. No runtime, registry, action contract or new configuration was introduced.

The same laboratory was read completely with `--output-limit 12000`,
`max_chars=12000` and both DOCX/XLSX packs loaded through `FunctionRegistry`.
Read outputs now omit redundant structure/defaults and repetitive font/size
records, while preserving node addresses, table coordinates, non-default styles,
emphasis, editability exceptions, OMML diagnostics and all existing text.

| Measurement | Previous read representation | Current read representation |
| --- | ---: | ---: |
| Useful text, Unicode characters | 4573 | 4573 |
| Full serialized JSON, summed over all portions | 259577 | 56659 |
| Paragraph records | 373 | 373 |
| Read portions | 21 | 5 |
| Portions rejected by registry's 12000 inner-result limit | 19 | 0 |

Sizes use `json.dumps(..., ensure_ascii=False)` with its default separators and
include `{"name":"docx_read","result":...}`. They are character counts, not
token counts, and exclude the history message's fixed textual prefix. To isolate
structural overhead, both sides use the same already-supported OMML text. The
previous representation and compact admission rule were replayed with local
test overrides on the real file; these are generated payload sizes, not sizes of
the tiny withholding messages in the original history. Maximum previous envelope
size was 13238. The current JSON total is **78.17% smaller**; ordered `(id, text)`
pairs match exactly. An intermediate measurement with the transport correction
but before compacting metadata was 261385 characters in 23 portions, confirming
that fitting the transport alone does not solve context overhead.

Independent reads of tables 1, 2 and 3 completed in 2, 2 and 1 calls respectively,
all admitted at the unchanged 12000 limit. Default `docx_inspect` also fits that
configuration. With read's default `max_chars=8000`, complete reading takes
8 admitted calls (58372 characters total, largest envelope 8000); with
`max_chars=12000`, the largest envelope is 11980. Literal `docx_read` search returns variant 8's
`y=sqrt(3(x)^(2)−9x+6)` at `word/document.xml:p55`. All 27 equations are represented;
the empty radical still has an explicit missing-argument warning. The source hash
above remains unchanged. This is a real-file registry replay, not a new live-model
completion of the entire laboratory.

Final validation: **363 full-suite tests passed** in the project environment
(25 existing optional skips), and **33 DOCX tests passed** separately with real
LibreOffice rendering. Regression coverage includes escaped Unicode text, high
run counts, full-envelope limits 12000/4000, max_chars 12000/8000/1800, the CLI
limit/default/frozen configuration, deterministic lossless continuation, empty
selections and unfittable metadata. Compact emphasis intervals are also checked
against exact-edit rejection across different source font sizes. `git diff --check`
and whitespace checks on the untracked pack/test files passed.

## Standard render availability regression

The laboratory run returned `dependency_missing` even though `soffice`,
`pdfinfo` and `pdftoppm` were available on host PATH. Reproduction through
`FunctionRegistry` with both renderer environment variables removed gave exactly
the reported error. The old pack rejected an unset `PAVLUSHA_DOCX_RENDERER`
before attempting any conversion. Its passing opt-in test explicitly assigned
the document skill's helper and a Python with rendering dependencies, so it did
not exercise ordinary launch configuration.

The pack now uses available host LibreOffice/Poppler without an external helper
or alternate Python. The explicit helper override remains supported. Missing
native tools/Pillow produce bounded `dependency_missing`; no packages are installed.
Only `tools/docx_functions.py`, `tests/test_docx_pack.py`,
`docs/docx-functions.md` and this report changed for this fix. Runtime/Core,
registry, State/recovery, XLSX and DOCX inspect/read/edit were unchanged.

The executed real smoke command, from the repository root, was:

```sh
.venv/bin/python /tmp/docx-render-runtime-smoke.py \
  --workdir /path/to/dogfood/work/xlsx-lab5-dogfood \
  --state-dir /path/to/dogfood/state/docx-render-default-smoke-20261006 \
  --functions tools/docx_functions.py --functions tools/xlsx_functions.py \
  --model scripted --worker-context-budget 40000 --max-tokens 1024 \
  --max-steps 6 --project-review-every 0 --output-limit 12000 \
  'Real DOCX render smoke'
```

The temporary harness removes both renderer environment variables and invokes
the real CLI `main()`. Only Worker replies are scripted: project initialization,
inspect, render, completion bookkeeping and finish. The production path
CLI -> Core action -> FunctionRegistry -> docx_render executes conversion and
artifact checks without mocks; any runtime shell fallback raises an assertion.
State is isolated from the existing laboratory session. This is not a new
live-model laboratory completion. Harness log: `/tmp/docx-render-runtime-smoke.log`.

Artifacts are under
`/path/to/dogfood/work/xlsx-lab5-dogfood/docx-render-default-smoke-20261006/`:
`document.pdf`, `page-1.png` through `page-8.png`, `manifest.json` and converter
logs. All eight pages were verified as readable PNGs; PDF has eight pages and
144-DPI rasterization. Pages 1 and 4 were visually sampled and contain legible
equations/text/logical symbols. This is an availability check, not full layout
certification; the public receipt correctly retains
`visual_verification=not_performed`. Source SHA-256 remained
`641779299f85b20be26fb1b9b23feaaaa9917ee478d0bb8cc75f1009d81452b5`.

Regression tests exercise actual standard rendering through the public registry
without either env override, including eleven pages to check padded Poppler
filenames. They also cover individually missing native tools, invalid explicit
overrides, refusal before rasterizing PDFs above 200 pages, and cleanup.
Existing override failure/timeout and real-helper tests still pass.
Project-Python targeted run: **36 tests, OK, one explicit helper-test skip**;
bundled document Python with both real render paths enabled: **36 tests, all pass**.
Full project suite: **366 tests, OK, 26 optional skips**. Full task diff reviewed;
`git diff --check` and untracked-file whitespace checks passed. No commit created.
