# DOCX custom function pack

`tools/docx_functions.py` is a trusted user module loaded through the existing
`--functions` mechanism. It exports exactly `docx_inspect`, `docx_read`,
`docx_edit`, `docx_render`. It uses the existing Pavlusha CLI parser to read
`--workdir`; it does not change the runtime or make semantic decisions. Worker
chooses what to read/change, interprets facts, calculates totals and evaluates
the rendered pages.

## Configuration

The existing runtime `--workdir` is the only document root. All function paths
are relative to its resolved **host** directory; shell/GUI see the same files at
`/work`. No separate root configuration is needed. The former
`PAVLUSHA_DOCX_ROOT` setting is ignored, even when present.
Put the trusted pack outside the Worker-writable directory.

At import the pack reads the launch arguments using `pavlusha_agent.cli.build_parser`,
including its existing `./agent-work` default, `--workdir PATH` and
`--workdir=PATH` forms. Relative workdirs resolve against the launch directory,
just as in runtime. The root is then frozen: later cwd/argument/environment changes
do not redirect document operations. Runtime creates a missing workdir before
Worker calls the functions. This pack targets the ordinary CLI entry point;
programmatic import/tests must supply the same CLI arguments in `sys.argv`.
No arguments, cwd, registry behavior or runtime policy are modified by the pack.
Renderer configuration and the existing runtime `--output-limit` are also fixed
at import. No separate transport-limit configuration is introduced.

```sh
PAVLUSHA_DOCX_RENDERER=/absolute/path/render_docx.py \
PAVLUSHA_DOCX_PYTHON=/absolute/path/document-python \
python agent.py --workdir /absolute/path/work \
  --functions /absolute/path/tools/docx_functions.py \
  "Read brief.docx, prepare an amended copy, reread it and check the pages."
```

For inspection, reading and editing, the ordinary launch needs only `--workdir`
and `--functions tools/docx_functions.py` (plus the usual model/task options):

```sh
python agent.py --workdir ./document-work --functions tools/docx_functions.py \
  "Inspect brief.docx and read its tables."
```

The controller Python needs `lxml`; it is the only external dependency used for
reading/editing. `python-docx` is used for test fixtures, not required by the
pack's restricted OOXML operations. Standard rendering additionally needs Pillow
in the controller, LibreOffice `soffice`, Poppler `pdfinfo`/`pdftoppm` on host PATH
and the document's fonts. It works without renderer environment variables or an
alternate Python, and does not require `pdf2image`. Missing tools/Pillow produce
a bounded `dependency_missing`; nothing is installed automatically.

`PAVLUSHA_DOCX_RENDERER` optionally overrides the standard path with an explicit
trusted helper; `PAVLUSHA_DOCX_PYTHON` selects that helper's Python (default: the
controller Python). A configured but missing helper/Python is an error, not a
silent fallback. The helper must support
`INPUT --output_dir DIR --emit_pdf --dpi DPI`, producing `source.pdf` and
`page-1.png`, `page-2.png`, etc. The document skill's `render_docx.py` meets this
contract and additionally needs `pdf2image`/Pillow in its Python.
`tools/requirements-docx.txt` lists optional pack/fixture/helper dependencies.
Rendering is Linux-only for atomic no-overwrite directory
publication (`renameat2`), consistent with Pavlusha's bubblewrap platform.

Every module and renderer is trusted host code, not sandboxed. Confinement checks
limit the document paths this pack accepts; they are not a security sandbox for
Python or a malicious renderer. Existing symlink escapes and `..`/absolute paths
are refused. These checks are not a defence against another process maliciously
racing filesystem directory/symlink changes during a call.

## Functions

```python
docx_inspect(path: str, cursor: str | None = None, limit: int = 40) -> dict
docx_read(path: str, revision: str, node_ids: list[str] | None = None,
          contains: str | None = None, cursor: str | None = None,
          max_chars: int = 8000) -> dict
docx_edit(path: str, output_path: str, revision: str,
          edits: list[dict[str, str]]) -> dict
docx_render(path: str, revision: str, output_dir: str,
            dpi: int = 144, timeout_seconds: int = 120) -> dict
```

All return JSON with `ok`. Expected domain failures return
`{"ok":false,"error":{"code":"...","message":"..."}}` inside the ordinary
function result. Unexpected implementation exceptions remain observable through
the existing Core `function_exception` mechanism. No function receives Core
objects or performs LLM calls.

### Inspection and addressing

`revision` is SHA-256 of the input bytes. IDs such as `word/document.xml:p5` or
`word/document.xml:t1` address XML-order paragraphs/tables within **that revision**;
there are no sessions or persistent node caches. A new revision requires new
inspection/reading before editing. Header/footer parts follow first-reference
order and linked parts appear once.

Inspect returns document-order body blocks, followed by referenced headers and
footers, paragraph/table/section counts, styles, short previews, editability and
coverage flags. Top-level tables have row/grid counts and merged/skipped-cell
flags; their contents are read using the table ID. Cursors paginate up to 40
inspection items per call. This is structure, not a generated semantic summary
or a claim about page count.

Read returns ordered paragraph segments with ID and text, non-default paragraph
style names, compact emphasis intervals and table-cell location. A table selector expands to its
physical cell paragraphs, including nested tables. Grid positions are zero-based;
column spans and vertical-merge continuations are reported. Horizontal merges
are not approximated by duplicating the same text across columns. Empty physical
cells remain visible through their empty paragraph records.

Without selectors, read traverses the covered content. `node_ids` selects at
most 64 paragraph/table IDs; `contains` performs a case-sensitive literal match
across run boundaries. They are mutually exclusive. Search returns the matching
paragraphs, not an interpretation of which occurrences matter. Follow
`next_cursor` with the **same revision and selectors** until null; max_chars may
change. Long paragraphs/runs are segmented without losing text. `max_chars` is
1800..12000 (default 8000) and bounds the **entire spaced JSON serialization**
of `{"name":"docx_read","result":...}`, including revision, metadata, items and
cursor. The effective budget is `min(max_chars, runtime --output-limit)`. This
matches the registry/runtime serialization and conservatively includes the outer
envelope; registry admission itself checks the inner result. The limit is read
from the same existing CLI arguments as workdir, with the CLI's existing default.
Programmatic callers must supply matching launch arguments and registry limits.
The public 12000 ceiling remains the pack's own safety cap, not an assumption
about the runtime limit. Smaller runtime limits produce smaller portions. If even
one node's metadata cannot fit, a compact `limit_too_small` error is returned;
JSON is never truncated. Empty selections are budget-checked too.

Reading deliberately omits redundant `kind`, `part`, `style_id`, physical
`cell_id`, fonts and font sizes. These are not a formatting snapshot for editing.
`id` retains the part and revision-bound paragraph address; inspect keeps its
existing structural metadata. `style_name` is present only for non-Normal styles.
Missing `editable` means true; false is explicit for complex paragraphs.
Whole paragraphs omit `text_offset` and `complete` (defaults 0 and true); partial
segments include both. `location` is omitted outside tables; within tables it
contains `table_id`, zero-based `row`/`column`, a non-default `column_span`
(default 1), and `vertical_merge` when applicable. Empty paragraphs/cells remain
addressable and visible.

`runs` is omitted when there is no explicit emphasis. It retains bold, italic,
underline and color intervals, with adjacent identical intervals coalesced;
absent emphasis attributes mean unspecified, not resolved inherited formatting.
Offsets refer to the full represented paragraph, including equations. Run
boundaries, fonts and sizes are checked against source XML by `docx_edit`,
independently of this compact reading view. Coalescing never weakens exact-edit
validation. Read coverage retains the partial flag, nonzero unsupported counts
and incomplete/omitted OMML warnings; full scope/count metadata remains in inspect.

### Word equations (OMML)

Inspect previews and read text include inline `m:oMath` and display
`m:oMathPara` equations in XML order, including table cells and referenced
headers/footers. The existing selectors, IDs, revision and cursors apply;
`contains` also searches the equation representation. No new functions or
dependencies are introduced.

The deliberately small reader supports math text runs, superscripts
`(base)^(exponent)`, square roots `sqrt(expression)`, indexed roots
`root(degree, expression)` and single-expression delimiters. It preserves
literal symbols and juxtaposition: `3(x)^(2)` means the document's `3x²`.
This is structural text, not executable syntax, LaTeX or a calculation.
Visual run/control formatting is ignored; unsupported mathematical styles
and structural properties are flagged. Fractions, subscripts, matrices,
accents, n-ary operators and multi-expression delimiters are not supported.

Paragraphs containing equations add `math_count` and `math_partial` to their
metadata. Inspect's `coverage.omml`, present when equations exist, reports
`formula_count`, `represented_count`, `partial_count`, `omitted_count`,
up to 12 distinct `warnings` and `warning_types_omitted`. Read includes the
incomplete/omitted counts and warnings when applicable, without repeating total
and represented counts. Unsupported
constructs retain available descendant text inside an explicit
`[OMML incomplete: ...]` representation; missing arguments use
`[OMML missing: ...]`. Unsupported control properties are reported through
coverage warnings. `coverage.partial` is true for incomplete or omitted math.
For a display wrapper containing several equations, partial counts
conservatively include all equations in that wrapper. Equations inside
excluded text boxes are counted as omitted rather than lost silently.

Ordinary Word run offsets include the length of preceding equation text;
equation spans do not acquire ordinary Word run formatting. Math paragraphs
remain non-editable under the existing exact-edit rules. Rendering is unchanged
and uses the original OOXML, not this textual representation.

### Editing

Each edit has only string `node_id`, `old`, `new`; supply 1..64 edits, at most one
per paragraph. `old` must be nonempty and occur exactly once, including overlapping
matches. Replacements are single-line text; tabs/newlines are not supported.
Two adjacent runs with identical explicit formatting can be crossed. Different
format-property XML is conservatively rejected even if a renderer might make it
look equivalent. New text inherits the affected formatting; surrounding runs,
paragraph/table properties and unused content remain intact.

Targets with fields, hyperlinks, bookmarks/comment anchors, drawings, tracked
changes, equations, formatting revisions or enclosing content controls are refused. Signed
packages cannot be edited. No paragraphs, rows, cells or sections are inserted;
no styles, fields or revisions are managed.

All edits are validated and applied to an in-memory snapshot before writing.
Only changed document/header/footer XML parts are serialized. Other ZIP member
**payload bytes**, names and metadata are copied, and untouched payloads are
verified before publication. Compression bytes/ZIP offsets and changed-part XML
serialization are not guaranteed identical. Output is a new file, atomically
published with a no-overwrite hard link on the same filesystem. Existing outputs
are errors. Parent directories must already exist. Failed edit batches do not
publish partial output; the source stays unchanged.

The receipt reports source/new revisions, changed parts, edited node IDs and
verified untouched-part count. Reread the output using its new revision to verify
content; the receipt is not a semantic approval.

### Rendering and checking

Render uses a snapshot of the supplied revision in a private temporary directory.
Without an override it converts through headless LibreOffice with a private
temporary profile, checks the PDF's 1..200 page count through `pdfinfo`, and
rasterizes through `pdftoppm`. Poppler's padded page suffixes are normalized to
the existing `page-1.png` naming; the profile is removed before publication.
Each command runs in its own process group under one shared 1..300 second deadline.
The override helper follows the same timeout/publication path. On timeout the
active group is terminated; the public output directory is not created.
DPI is 72..200. Source bytes are never rewritten by the conversion.

Before atomically publishing the directory, the pack requires a nonempty PDF
with PDF header and a complete sequence of 1..200 structurally readable PNGs.
The manifest contains source revision, page list, DPI, renderer name and warnings;
the compact function result returns manifest/PDF paths and a page-name pattern.
These are mechanical artifact checks, not full PDF validation, layout approval,
field updating or proof of identical rendering in Microsoft Word.

`visual_verification` is always `not_performed`. JSON file paths do not attach
images to Worker context. Use the existing default GUI capability (unless
disabled with `--no-gui`), `gui_start`, `view_gui` and
other GUI actions to inspect every page, zooming where needed. Alternatively a
human must inspect them. A successful render alone must never be reported as a
successful visual check. Comments may not render and cached field values can be
stale; the pack reports these constructs rather than interpreting them.

## Errors and limits

Typical codes: `configuration_error`, `path_escape`, `invalid_path`,
`file_not_found`, `invalid_docx`, `document_limit_exceeded`, `dependency_missing`,
`stale_revision`, `unknown_node`, `invalid_selector`, `invalid_cursor`,
`limit_too_small`, `invalid_limit`, `invalid_edit`, `text_not_found`,
`ambiguous_match`, `format_conflict`, `unsupported_target`, `destination_exists`,
`result_limit_exceeded`, `io_error`, `write_failed`, `invalid_render_options`,
`render_failed`, `render_timeout`. Error messages are bounded.

Input limits: 32 MiB ZIP, 128 MiB expanded payloads, 2048 parts, 50000 addressable
elements per covered part. All public results have a 12000-character compact-JSON
maximum; read additionally obeys the smaller full-envelope transport budget
described above. Other functions retain their existing limits. The pack does not
return images, full XML or megabytes of document text into prompts.

Only conventional WordprocessingML DOCX packages with `word/document.xml` are
accepted. Text boxes and footnotes/endnotes/comments are outside the text-reading
scope. Fields, revisions, content controls and hyperlinks have coverage flags;
plain/cached text is not advertised as a fully resolved Word view. It does not
perform OCR, infer headings, compute budgets, choose replacement occurrences,
modify styles or certify document correctness.

## Tests and dogfood

Run `python -m unittest tests.test_docx_pack -v` with the document dependencies.
`PAVLUSHA_TEST_REAL_RENDER=1` additionally exercises the configured local
LibreOffice renderer. The real-render test uses `PAVLUSHA_DOCX_RENDERER` when
provided; without an available renderer it is explicitly skipped. Existing
runtime tests remain independent of optional DOCX dependencies.

The initial verification completed with 21 targeted tests passing, including real rendering.
The ordinary controller environment's full suite ran 330 tests successfully with
26 skips: 20 DOCX tests lacked optional document dependencies there and were all
run separately with the bundled document Python; 6 were pre-existing skips.
Compile/import sanity in both Python environments and `git diff --check` passed.
See [the real Worker dogfood report](docx-dogfood.md) for observations, independent
checks, metrics and the reason no equivalent shell baseline was run.

Workdir integration regression tests additionally cover the ordinary CLI launch
with `--functions tools/docx_functions.py`, inspection/reading/editing, the default
adjacent State directory, both workdir spellings, relative/default/repeated workdirs,
frozen root and ignored legacy root environment setting. Absolute paths, `..` and
symlink escapes are rejected for source paths and edit/render destinations.

After the workdir-only change, all 23 targeted tests passed, including real rendering.
The full controller suite passed 332 tests with 27 skips: 21 tests require optional
DOCX fixture dependencies and passed separately in the document Python environment;
6 are existing project skips. The CLI integration uses scripted Worker replies and
runs the real CLI/runtime/registry; it does not claim a new live-model dogfood run.
