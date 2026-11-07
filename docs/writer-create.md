# Writer document tools

The Office extension provides bounded ODT operations via LibreOffice Writer.
All modifications save a new copy under the work directory; outputs never
replace existing files. Use `odt_inspect` to get the current SHA-256 revision
before each operation. Writer round trips can change complex document features;
inspect and render results before accepting them. Writer inspect and edit open
source documents with macros and external updates disabled. Rendering uses a
separate LibreOffice profile and the command-line converter; it does not set
the UNO load flags used by inspect/edit.

`odt_inspect` and `odt_read` return top-level paragraphs and tables (including
cell text); `odt_edit` replaces exact text in a top-level paragraph using its
revision-bound `node_id`. `odt_render` exports PDF and page PNGs. Inspect/read do not currently cover headers, footers or
equations, and edits inside table cells are not supported.

## Create and format text

`odt_create` creates a document from an ordered list of paragraphs with
`text`, `role` (`title` or `body`), and optional `align`. One `font` and `size`
apply to the whole new document (defaults: Liberation Serif and 14 pt).
Titles are bold; body paragraphs are not. Lines are single-spaced. Body paragraphs
default to a 1.25 cm first-line indent; titles have no first-line indent. Other
paragraph indents and spacing default to zero, and body text defaults to
justified. Explicit global paragraph spacing and first-line indent overrides
remain available when requested. Text must be supplied as separate paragraphs.

`odt_format_paragraph` changes the alignment or role (`title`/`body`) of one
existing top-level paragraph. `match_text` is its exact, complete text and must
match exactly one paragraph. Marking a paragraph as title makes it bold; body
removes bold and italics for that paragraph.

`odt_normalize` applies one font and size, single spacing, zero paragraph
spacing and a 1.25 cm first-line indent only to ordinary body paragraphs.
Titles, list items, captions and table cells have no first-line indent. It removes italics and
keeps bold only for title/heading styles and repeating table-header rows.
Alignment applies to body paragraphs; existing heading and table-cell alignment
is retained. Headers, footers and complex objects are not normalized.

## Insert content

`odt_insert_paragraph`, `odt_insert_list`, `odt_add_table`, and
`odt_add_image` accept optional `after_text`: the exact complete text of a
unique top-level paragraph. The new content goes after it. If `after_text` is
omitted, content is appended at the end. Duplicate or missing matches are
rejected without producing an output. Tables, pictures and lists inside other
containers are not anchor targets.

`odt_insert_list` creates a single-level bullet or numbered list. Its items are
ordinary text; nested lists are not currently supported.

`odt_add_table` inserts up to 50 rows and 10 columns of text. `header` defaults
to true and makes the first row bold and repeat on page breaks. Optional
`alignment` and `caption` control cell text and the caption above the table.
`odt_replace_table` replaces the dimensions and text of one top-level table,
selected by its 1-based order in the body. It replaces all table cell content;
it does not provide per-cell editing. Merged or complex tables are not a
supported target.

`odt_add_image` inserts PNG or JPEG from the work directory. `width_cm` defaults
to 12 and is limited to 1–16 cm; aspect ratio is preserved. Optional `alignment`,
`alt_text`, and `caption` control placement, accessibility and the caption
below the image.

## Deferred

Page margins and orientation, page breaks, headers, footers, page numbers,
individual table-cell operations, nested lists, italic emphasis, and special
handling for complex document features are deferred. The sandbox requires
LibreOffice Writer, Python UNO, Pillow and bubblewrap. Path checks are not a
defence against malicious concurrent filesystem changes.
