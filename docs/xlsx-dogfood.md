# XLSX pack dogfood

## Design and task

The pack follows DOCX's trusted synchronous, revision-based, bounded document
capability boundary. XLSX addressing uses sheet names/A1 rectangles rather than
DOCX node IDs; values, formulas, cached results and presentation metadata remain
distinct. Four endpoints suffice: inspect, read, edit and render. There is no
formula engine, semantic analysis, session or duplicated single-cell/search API.

Rendering uses already available LibreOffice/Poppler; those remain optional native
dependencies. Ordinary reading/editing requires only the optional Python packages.
Point edits patch OOXML to avoid openpyxl's full-package preservation limitations.
An engine-rewritten recalculated copy is separate from the edited deliverable.

The real local `qwen/qwen3.8-27b` Worker received a small Russian lab experiment
plan, not cell addresses or an edit plan. The instruction changed series A from
four repeats at 1.5 hours to six at 1.75 hours, and series B from 2 to 2.5 hours per
repeat, retaining its repeat count. Historical row and hidden archive, formulas
and formatting had to remain intact. The Worker had to inspect/read, batch-edit,
reread the new revision, render, read engine results and examine the page through
the existing GUI before finishing. Shell processing was explicitly excluded.

Fixture and ordinary HTML image viewer were supplied by the harness. Artifact Tool
created the initial workbook/formulas and baseline image; fixture-only OOXML setup
provided print settings and a `veryHidden` archive. The source had a merged title,
styled inputs, formula totals, decimal formats and explicit column/row sizes.
Both `--functions tools/docx_functions.py` and `--functions tools/xlsx_functions.py`
were loaded through unchanged repeatable CLI/registry; the Worker used XLSX functions.
Only the normal `--workdir` supplied the document root. The existing optional `--gui`
provided visual observations; no runtime, Core, State, recovery, shell, provider,
registry or Worker action changes were made for this task.

## Observations

| Measurement | Observed |
| --- | --- |
| Model | qwen/qwen3.8-27b, low reasoning |
| Worker steps/actions | 13 |
| Custom-function calls | 8, exercising all four functions |
| Shell actions / Worker-written document glue | 0 / 0 |
| GUI actions | gui_start, gui_close; initial image observation contained the single page |
| Elapsed runtime | 174.2 seconds |
| Prompt tokens | 4,081 initially; maximum 19,573 |
| Sum of prompt tokens across turns | 149,859, including repeated context |
| Completion tokens across turns | 3,875 |
| Domain failures / recovery retries | none observed |
| Rendered pages | 1 |

Worker independently chose `План!B4`, `C4`, `C5` and applied one three-edit batch:
4→6, 1.5→1.75 and 2→2.5. `B5=3` stayed unchanged. A read with the output revision
confirmed these values and intact `=B4*C4`, `=B5*C5`, `=SUM(D4:D5)` formulas, with
their caches intentionally null until actual recalculation.

Calc produced 10.5 hours for A, 7.5 for B and 18 total in the separate derivative.
Worker reread its cached results with the returned derivative revision. GUI showed
the rendered page, and Worker reported reviewing it. A separate parent QA pass
opened the actual full-resolution page PNG; headings, inputs, totals and historical
row were readable without clipping or overlapping content. Hidden archive was not
printed; this visual view does not certify hidden data or arbitrary out-of-print cells.
The function's manifest correctly retained `visual_verification=not_performed`.

Independent checks verified source SHA-256 remained unchanged, all three edits,
formula text, exact style identities throughout both sheets, historical cells and
the `veryHidden` archive. ZIP comparison found only `xl/workbook.xml` and
`xl/worksheets/sheet1.xml` changed; every other member payload, including archive
sheet and styles, was identical. Manifest hashes match the edited/engine-derived
workbooks. Worker reached accepted FINISH and the normal controller recorded its
ordinary operation/Project State evidence.

Local artifacts/logs are under `/tmp/pavlusha-xlsx-dogfood`: `work/experiments.xlsx`,
`work/revised.xlsx`, `work/qa/`, `live.txt` and `state/` telemetry. They are disposable
verification evidence, not repository dependencies or required user files.

## Limitations found

The original `0.0` format displays the new 1.75 input as **1.8**, while its underlying
value remains 1.75 and the correct calculation is 10.5. Worker explicitly recognized
the rounding during GUI reasoning, but its final summary did not mention it.
Preserving format is not proof that a new value's precision is fully visible.
The minimal point editor does not edit number formats; inspect/read exposes the
format so Worker can report this limitation. No semantic correction was hidden in
the function. Future format-edit support would need a separately bounded design.

Broad reads return substantial per-cell layout metadata; narrower rectangles reduce
context. A large cell's text is explicitly truncated rather than segmented. The
default reader cannot compute displayed strings, conditional-format outcomes or
Excel formulas; a non-null original cache cannot be treated as fresh calculation.

LibreOffice conversion rewrites its derivative and does not certify Excel fidelity
or every formula/native feature. Pivots/chart caches, external data, arrays/spills,
macros and universal Excel automation are not managed. Edited-copy preservation and
derivative rendering have deliberately different guarantees. Only the workbook's
print view was inspected. No equivalently provisioned shell baseline was run;
these metrics describe one successful pack workflow, not a speed advantage claim.

## Verification

21 targeted XLSX tests passed with real LibreOffice/Poppler. They cover sheet/layout
inspection, hidden/merged data, caches/formulas, numeric formats, bounded cursors,
stale revisions, point value/formula/date/blank edits, exact untouched ZIP/chart/style
preservation, failure atomicity, traversal/symlink escapes, malformed/macro/oversized
inputs, protected/shared/array formula handling, calcChain cleanup, external connection
refusal, timeout/no-clobber render publication, actual engine caches and hidden-sheet
print scope. Registry/CLI tests cover both DOCX/XLSX loading orders and runtime with
packs absent. Full suite passed 353 tests, with 25 skips: 18 XLSX tests required the
optional document Python and passed separately, 6 were pre-existing skips and 1 was
the opt-in DOCX real-render test. No packages were installed during this task.
