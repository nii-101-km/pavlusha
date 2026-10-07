# XLSX custom function pack

`tools/xlsx_functions.py` exports four trusted synchronous functions through the
existing repeatable `--functions`. Worker chooses ranges, interprets data, performs
business analysis, selects edits and evaluates images. The pack supplies mechanical
workbook capabilities, not an agent, spreadsheet engine, session or document State.
Core, runtime, Worker contracts, recovery, shell and the registry are unchanged.

## Dependencies and launch

Reading/editing needs only the optional Python packages in
`tools/requirements-xlsx.txt` (`openpyxl>=3.1`, `lxml>=5`). LibreOffice is **not** a
requirement for Pavlusha, loading this pack, inspecting, reading or editing XLSX.
Missing packages produce `dependency_missing` on calls rather than automatic installs.

```sh
python agent.py --workdir ./work --functions tools/xlsx_functions.py \
  "Read experiments.xlsx and prepare an amended copy."
```

Compose independently with DOCX; exported names remain globally unique:

```sh
python agent.py --workdir ./work \
  --functions tools/docx_functions.py --functions tools/xlsx_functions.py \
  "Check the spreadsheet and use the verified figures in the document."
```

As in the DOCX precedent, the pack uses the existing CLI parser at import to
resolve and freeze the launch `--workdir`, including its existing default and
relative/equals forms. It changes neither cwd nor launch arguments. Programmatic
imports must supply that same CLI configuration in `sys.argv`. There is no root
environment variable or new CLI option. Keep trusted modules outside Worker-writable
workdir. All document/output paths are relative to the host workdir; GUI/shell see
the same files at `/work`. Absolute paths, `..`, root selections and symlink escapes
are refused. These checks are not a Python sandbox or a defence against malicious
concurrent directory/symlink replacement.

Only optional rendering additionally needs LibreOffice `soffice` and Poppler
`pdfinfo`/`pdftoppm` on the controller's ordinary PATH, plus
`tools/requirements-xlsx-render.txt` (`Pillow`, `pdf2image`). `soffice` is resolved
at import. No executable discovery flags, bundled downloads or installations are
added. This is the same optional native office dependency already used by the
DOCX renderer. Without it `xlsx_render` reports `dependency_missing`; other
functions remain usable. Local validation used the already available desktop
dependency bundle by prepending its `bin/override` directory to PATH.

## API

```python
xlsx_inspect(path: str, sheet: str | None = None,
             cursor: str | None = None, limit: int = 20) -> dict
xlsx_read(path: str, revision: str, sheet: str, cell_range: str,
          cursor: str | None = None, max_chars: int = 8000) -> dict
xlsx_edit(path: str, output_path: str, revision: str,
          edits: list[dict]) -> dict
xlsx_render(path: str, revision: str, output_dir: str,
            dpi: int = 120, timeout_seconds: int = 120) -> dict
```

There is no duplicate `search`, `get_cell`, formula evaluator, semantic validator,
or separate recalculate endpoint. Explicit bounded ranges serve single cells and
tables; Worker can choose the next range after inspection. Rendering already
requires Calc, so its recalculated derivative exposes the same calculation once
without adding another public operation.

### Inspect and read

Inspection without `sheet` returns workbook coverage and ordered worksheet names,
visibility (`visible`/`hidden`/`veryHidden`), occupied extent, stored/formula counts,
merged-range count, freeze panes and sheet protection. Extent includes formatting
and is not claimed to be a semantic data table. With `sheet`, inspection paginates
layout records: merges/anchors, explicit row/column dimensions, hidden/grouped
elements, default sizes, print area/titles/page setup, filters, named ranges,
tables, validation and conditional-format locations. Features such as charts,
drawings, pivots, connections, external links, extensions and comments are counted
or flagged; they are not fully interpreted. Reader warnings are exposed.

Read requires SHA-256 `revision` of actual workbook bytes and an exact sheet name.
`cell_range` is uppercase `A1` or a rectangle such as `A1:D30`, without `$`, whole
rows/columns, sheet qualifiers, named expressions or unions. Rectangles are limited
to 4096 cells. Row-major output includes blanks and merged followers (not repeated
anchor values), cell address, underlying value, formula, number format, style ID,
merge/anchor, hidden row/column hints, explicit size overrides, bold/wrap/alignment,
hyperlinks and comments. Conditional formatting is not computed by this reader.
Dates/times use typed ISO objects and durations seconds; they are not guessed from
display strings. Default sizes are available through inspection. Explicit numeric
formats are reported, not converted into Excel-identical displayed strings.

Formula text and stored cached values are separate. A cache can be missing or
stale even if it is non-null. Every read states this; openpyxl never evaluates
formulas. Special array/data-table formulas are identified, not flattened into a
claimed ordinary formula. Text/formulas/comments above 1000 characters return
`{text,truncated:true,length}` instead of silently claiming complete content;
this MVP does not segment individual long cells.

Follow `next_cursor` with the same selector and revision until null; `max_chars`
may change. Cursors encode a revision/query/index, have no server session, and
are not security tokens. Inspect obtains the revision itself; its cursors detect
revision changes. Read/edit/render reject stale input before acting.

### Point edits and preservation

Supply 1..64 edits, one per distinct sheet/cell, with exactly one content field:

```json
[
  {"sheet":"Plan","cell":"B4","value":6},
  {"sheet":"Plan","cell":"D6","formula":"=SUM(D4:D5)"},
  {"sheet":"Plan","cell":"A9","date":"2026-10-06"}
]
```

`value` is null (clear contents), boolean, finite number or literal string,
including a string starting with `=`. Only `formula` introduces a formula; it
must begin with `=` and use Excel syntax. The pack does not validate semantic
formula correctness. This MVP refuses new `[]` external/structured references
rather than attempting their management. `date` accepts ISO dates/naive datetimes;
existing number format is retained, so choose a suitably formatted target when
display matters. New sparse cells/rows are allowed; sheet structure is not shifted.

Only a merged anchor is writable. Protected sheets and special/shared formula
targets are refused. Entire books with array/spill/data-table formulas are refused
for edits because safely invalidating their dependent cached output regions needs
more than the minimal point editor. No row/column insertion, sheet management,
style changes, chart/pivot editing, VBA, Power Query or universal Excel automation.
Macro packages, XLS/XLSM, non-worksheet tabs and nonconventional SpreadsheetML
are outside scope. Signed or externally connected books can be inspected/read,
but cannot be edited or sent to the renderer.

The editor patches worksheet XML without saving through openpyxl. A full [openpyxl round trip](https://openpyxl.readthedocs.io/en/3.1/tutorial.html)
can lose unsupported shapes/features; the chosen approach preserves
all untouched ZIP member payloads and metadata, including styles, drawings,
charts, validation and relationships. Modified XML and ZIP compression streams
need not be byte-identical. Source bytes are unchanged.

After any batch, **all workbook formula caches** are invalidated, including
cross-sheet ones; formula text is preserved. `fullCalcOnLoad`/`forceFullCalc` are
requested and calcChain part/references removed. Existing iteration/manual settings
are retained; these flags do not prove a subsequent application will recalculate.
Pivot/chart caches are preserved, not refreshed; validation rules are not enforced.
The receipt reports changed/removed parts and preservation counts. Reread the new
revision to check values and formula text, then use an actual engine if computed
results matter. No dependency graph, formula engine or hidden recalc is implemented.

All edits and the receipt are validated before atomic no-overwrite file publication
using a same-filesystem temporary file/hard link. Output must be a new `.xlsx` and
its parent must already exist. Batch failure leaves no public partial copy.

### Recalculation, render and visual verification

Render snapshots the supplied revision and runs headless Calc with an isolated
temporary user profile. It first saves a recalculated XLSX derivative, then exports
its print layout to PDF and rasterizes every PDF page through Poppler. All converter
processes have bounded timeouts; Calc runs in its own process group and is killed
on timeout. Source and point-edited copy are never overwritten.

The derivative is a **LibreOffice rewrite**, not the payload-preserving edited
deliverable. It may change features, formula syntax, number formatting and caches;
Excel and Calc are not identical, and volatile/time-dependent formulas are not
deterministic. A successful conversion is not proof every formula is correct.
Use returned `recalculated_path`/`recalculated_revision` with `xlsx_read` to examine
actual engine caches and error values. Preserve the edited copy separately when
fidelity matters.

Rendering respects the workbook's visible sheets and print areas/page setup. It
does not use [SinglePageSheets](https://help.libreoffice.org/latest/en-US/text/shared/guide/pdf_params.html), which overrides print ranges and includes hidden
sheets. It provides a print view, not a viewport of every arbitrary range/hidden
cell. Read hidden data separately; cells outside print areas are not visually
certified. There is no claimed sheet-to-PDF-page mapping. Small useful print areas
and sane page setup avoid huge printouts; rendering does not silently restyle them.

The published directory contains `recalculated.xlsx`, `workbook.pdf`,
`page-1.png` etc., `manifest.json` and conversion log. A manifest binds source
and derivative revisions, DPI and page count. PDF/page count and PNG integrity
are checked mechanically. Linux `renameat2(RENAME_NOREPLACE)` publishes the whole
directory atomically without replacing an existing destination.

`visual_verification` is always `not_performed`. Return paths do not attach images
to Worker context. Inspect actual page images through existing default GUI actions
(`--no-gui` disables them)
or a human viewer. No new image-observation or Worker interface is introduced.

## Errors and limits

Expected errors return `{ok:false,error:{code,message}}` inside the ordinary
function result. Unexpected exceptions retain generic `function_exception`.
Codes include `path_escape`, `invalid_path`, `file_not_found`, `invalid_xlsx`,
`unsupported_workbook`, `unsupported_target`, `dependency_missing`, `stale_revision`,
`unknown_sheet`, `invalid_range`, `range_limit_exceeded`, `invalid_cursor`,
`invalid_limit`, `invalid_edit`, `merged_cell`, `destination_exists`,
`workbook_limit_exceeded`, `result_limit_exceeded`, `write_failed`, `io_error`,
`invalid_render_options`, `render_failed`, `render_timeout`, `render_limit_exceeded`.

Limits: 32 MiB input/output workbook, 128 MiB expanded ZIP, 2048 ZIP parts,
128 worksheets, 200000 stored cells plus merged area. No DTD/entity expansion;
XML is admitted before openpyxl. Results are JSON-compatible and bounded to 12000
characters; read budget 1800..12000, inspect 1..40 records per call. Core's existing
output admission limit also applies. Render: 72..200 DPI, 1..300 seconds and 1..100
PDF pages. These are practical admission bounds, not hard host CPU/memory quotas.
Trusted Python/office subprocesses run with controller permissions, not shell
confinement; `--network` does not sandbox custom functions.

## Tests

```sh
python -m unittest tests.test_xlsx_pack -v
# Opt in only with available local LibreOffice/Poppler:
PAVLUSHA_TEST_REAL_XLSX_RENDER=1 python -m unittest tests.test_xlsx_pack -v
```

Optional dependency tests are explicitly skipped when openpyxl/lxml is absent.
Registry/root tests remain dependency-independent. The full existing suite checks
runtime behavior with packs disabled as well as custom-function dispatch/recovery.
See [the dogfood report](xlsx-dogfood.md) for live observations and limitations.

Initial verification: all 21 targeted XLSX tests passed with real local conversion;
the ordinary full suite passed 353 tests with 25 skips (18 XLSX dependency tests
verified separately, 6 existing project skips, and 1 opt-in DOCX render test).
The XLSX registry tests also cover DOCX/XLSX module composition in both orders,
workdir-only root configuration and disabled runtime's original shell flow.
