# Experimental LibreOffice Calc adapter

`pavlusha_modules.office.calc_adapter` defines a `SpreadsheetAdapter` protocol
and a LibreOffice adapter. Standard ODS inspect, read, edit and render tools
use LibreOffice. To expose additional chart and surface tools, set `enable_calc`
on the Office module entry in the module manifest:

```json
{"factory": "pavlusha_modules.office:create_extension", "settings": {"enable_calc": true}}
```

Use `ods_create` for a new blank workbook. Use `ods_inspect` to get the
revision of an existing workbook, then `ods_read` and `ods_edit`.
Edits accept a scalar `value`, a `formula`, or an ISO `date`. Numbers and
booleans are written as numeric Calc values. Editing produces a new file,
recalculates it, and may change complex workbook features. The source remains unchanged.

The adapter reads an A1 range or edits a copy, asks Calc to recalculate, and
saves a new ODS. `revision()` computes the required SHA-256 revision. The source
is snapshotted before the worker opens it, and publication refuses overwrite.
The worker uses a separate LibreOffice profile, disables macro execution and
external update requests, and closes its document and process after each call.

Requirements: a working LibreOffice installation **including Calc**, a Python
interpreter with the matching `uno` module, and local UNO pipe access. The
adapter's `python` and `soffice` arguments can select those executables. It is
experimental for complex workbooks: a LibreOffice round trip may alter
workbook features, and calculated values differ from the former cached-value reader. It
has not been validated on protected sheets, merged cells, external links,
complex formulas or arbitrary workbooks. The path checks do not defend against
concurrent filesystem races; don't use with untrusted concurrent writers.

The Calc adapter launches its Python UNO worker and LibreOffice as host user
processes, outside Pavlusha's bubblewrap sandbox, with the controller's environment
and network permissions. File path validation and disabled macros/external updates
are not a complete isolation boundary for untrusted documents. By contrast, Writer
edit/create, surface generation, and the opt-in Basic runner use the shell sandbox.
Chart, read, inspect, edit and create operations use the host Calc adapter.

```python
from pathlib import Path
from pavlusha_modules.office.calc_adapter import LibreOfficeCalcAdapter, revision

root = Path('/path/to/workdir')
calc = LibreOfficeCalcAdapter(root)
rev = revision(root / 'input.ods')
print(calc.read('input.ods', rev, 'Sheet1', 'A1:B2'))
print(calc.edit('input.ods', 'output.ods', rev,
                [{'sheet': 'Sheet1', 'cell': 'A1', 'value': 5}]))
```

A basic end-to-end test on LibreOffice Calc 24.2 verified range reading, editing a
copy, and recalculation of a simple formula. Run it with
`PAVLUSHA_TEST_CALC=1 python -m unittest tests.test_calc_adapter`. The test also
checks one bold style, a merged range, and an untouched second sheet; this does
not establish preservation of arbitrary workbook features. Further compatibility
and preservation tests are needed for complex workbooks.

## Experimental Basic macros

Set `enable_basic_macro: true` on the Office module entry to expose
`calc_basic_macro(code, output_path)`. It runs a LibreOffice Basic `Sub Main`
on a **new** Calc document using `ThisComponent`, then embeds the Basic module
and saves a new `.ods` file. It does not open existing documents or overwrite a
file. The macro runs in Pavlusha's bubblewrap shell environment with networking
disabled, an isolated LibreOffice profile, and a 60 second timeout. The whole
Pavlusha work directory is writable by the macro: use a separate work directory
for untrusted or experimental macros. The resulting ODS contains executable
code; enabling macros when opening it later is a separate trust decision.

```json
{"factory":"pavlusha_modules.office:create_extension",
 "settings":{"enable_basic_macro":true}}
```

The runner requires LibreOffice Calc, `/usr/bin/python3` with `uno`, and a
working bubblewrap installation. No macro tool is registered by default. The
ordinary ODS tools open documents with `MacroExecMode.NEVER_EXECUTE`.

### Solver in LibreOffice Basic

With `com.sun.star.comp.Calc.LpsolveSolver`, `Objective` is a
`com.sun.star.table.CellAddress`, not a cell object; obtain it with
`sheet.getCellRangeByName("B3").getCellAddress()`. `Variables` is an array of
`CellAddress` structs and `Constraints` an array of `SolverConstraint` structs.
Declare typed arrays in Basic with `Dim vars(1) As New
com.sun.star.table.CellAddress` and `Dim cons(1) As New
com.sun.star.sheet.SolverConstraint`, then assign `solver.Variables = vars()`
and `solver.Constraints = cons()`. Each constraint's `Left` is a cell address;
`Right` is a double or another cell address. Set `Document`, `Maximize`, and,
when appropriate, the solver property `NonNegative`. Call `solve()` and check
`solver.Success` before using `solver.Solution` and `solver.ResultValue`.

A linear model with two variables and two upper bounds was verified end to end
on the installed LibreOffice Calc. A successful `calc_basic_macro` result means
the Basic entry point returned: it does not prove a solver succeeded or that a
macro's own error handler did not swallow a failure. Inspect the saved result.

Known issue: LibreOffice Basic compilation errors can return from the script
invocation without an exception, leaving an empty workbook even when the tool
reports `ok`. In particular, Basic strings escape embedded quotes by doubling
them (`""`), not with a backslash (`\"`). Until compilation failures are
reported reliably, validate the workbook contents and do not treat `ok` as proof
that the macro body ran. This remains an open defect in the macro runner.

## Charts

With `enable_calc: true`, Office also provides `ods_chart_create`,
`ods_chart_list`, and `ods_chart_export`:

- `ods_chart_create` writes a new ODS copy with a `scatter`, `line`, `column`,
  or `pie` chart. The source data is a rectangle on the same sheet: its first
  column contains X values or categories; later columns are series. The first
  row is treated as headers by default. Pie charts accept exactly two columns.
  Supply a top-left `anchor` cell, optional title, axis titles, and legend.
- `ods_chart_list` lists charts in draw order using one-based indices. Saving
  may change LibreOffice's internal object names, so export uses the
  index returned by this operation rather than relying on the creation name.
- `ods_chart_export` saves the selected chart as a new PNG in a relative path
  inside the work directory. The parent folder must exist. The separate
  `view_image` tool can inspect the exported PNG when the images module is
  enabled. Both outputs refuse overwrite and require the current ODS revision.

The chart is inserted through LibreOffice, so a round trip can alter other
workbook features. Automatic point-value labels are not yet supported: a Calc
export with labels was observed to clip the chart. Chart title, axis titles,
and legend are supported.

## 3D surface for Calc (ODS)

With `enable_calc: true`, `calc_ods_inspect(path, sheet?, cell_range?)` returns
an ODS revision, sheet names and dimensions, and optionally up to 100 cells of
values and formulas. `calc_surface_create` creates a new ODS copy containing a
static 3D surface picture, and saves the same picture as a separate PNG.

The selected range has an unused top-left cell, numeric X coordinates across the
first row, numeric Y coordinates down the first column, and numeric Z values in
the remaining cells. X and Y coordinates must strictly increase; 2–50 values
are accepted per axis. `path`, `output_path`, `image_path`, `revision`, `sheet`,
`cell_range`, and `anchor` are required. Optional `title`, `x_title`, `y_title`,
and `z_title` label the image. Both outputs must be new paths inside the work
directory, with existing parent folders. Source macros are disabled on open.

This is an embedded **static image**, not a native editable or rotatable Calc
chart. Re-run the operation after changing the data. Calc 24.2 was observed to
interpret an Excel native surface chart as a 3D column chart; this tool targets
ODS and does not create or export Excel surface charts. LibreOffice and Python
with UNO and Pillow must be available inside the bubblewrap environment.
