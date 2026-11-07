# Pavlusha 0.9.0 — draft

This document describes the working tree being prepared for release. It does
not announce a published release. The previous release is [0.8.0](release-0.8.0.md).

## Highlights

- LibreOffice Writer and Calc tools use native ODT and ODS files.
- An optional draw.io module creates editable diagrams and exports PNG previews.
- Viewed images can remain in the current context across turns.
- Experimental bounded Python programs can call explicitly permitted tools.
- A managed local llama-server can be retained for sequential sessions.

## Added

- Writer tools for creating, inspecting, editing, formatting and rendering ODT;
  paragraphs, single-level lists, tables and PNG/JPEG images are supported.
- Calc tools for creating, inspecting, editing, recalculating and rendering ODS;
  optional chart creation/export and static 3D surface images are available.
- Optional Basic macro execution in a new Calc document, disabled by default.
- The separate [draw.io module](drawio-module.md), [programmatic execution](programmatic-module.md)
  and [persistent server mode](persistent-server.md).

## Changed

- `view_image` observations are retained in the current context history until a
  context reset; the files and journal remain available after reset. This uses
  context space and can substantially increase session storage.
- The Qwen example includes a compatible `mmproj` placeholder, an example 110k
  context, medium reasoning and no separate reasoning-token limit. The user must
  provide compatible model files, server options and sufficient resources.
- Process lifecycle handling and tool contracts support bounded programmatic
  calls and persistent-server management. Other modules remain optional.

## Fixed

- Viewed images no longer disappear from the model's context immediately after
  the next request, allowing comparisons across subsequent turns.
- Ordinary Writer body paragraphs receive a first-line indent by default;
  headings, lists and table cells do not receive it during normalization.

## Breaking Changes

- The former DOCX/XLSX tools and their direct OOXML implementations were removed.
  The Office module exposes ODT/ODS tools and does not provide Word/Excel export.
  Existing configurations, programs and sessions that invoke removed tool names
  require changes. A LibreOffice save may affect complex document features.
- Inspection/editing of headers, footers, equations and individual Writer table
  cells is not provided by the current ODT tools.

## Experimental

The LibreOffice adapters, Basic macros, programmatic execution, persistent
server reuse and draw.io integration should be evaluated against their documented
constraints. Modules are trusted code. Programmatic allowlists are not a complete
isolation boundary: an allowed tool can have its own privileges, including host
process and network access. See [programmatic execution](programmatic-module.md),
[Calc](libreoffice-adapter-prototype.md) and [draw.io](drawio-module.md).

## Known Limitations

- Basic compilation errors can be reported as success; inspect the generated ODS
  and independently verify the result.
- A program timeout cannot interrupt a tool call already executing; it may have
  side effects even if a call fails or its result is unavailable.
- Calc and draw.io launch external programs with host-user permissions. The Basic
  runner has access to the entire work directory even when network is disabled.
- Image observations consume context and session storage, and are not retained
  automatically after context reset.
- 3D Calc surfaces are static images, not native interactive charts.
- No named-scenario library, version history or snapshotting is provided. Ordinary
  `.py` files can be kept in the work project and read inside `execute_program`;
  see [programmatic execution](programmatic-module.md) for permissions and logging.
  There is no persistent background agent or execution of commands inside a single
  ongoing reasoning generation.

## Testing

On the local pre-release checkout, the full suite ran 275 tests: 262 passed and
13 were skipped (11 opt-in LibreOffice integration cases and 2 live-network
cases). A separate LibreOffice run with both integration flags enabled passed 21
tests, including those 11 cases, on LibreOffice 24.2.7.2. Of the two skipped network
cases, the loopback isolation case passed in a separate local-only run; the case
connecting to an external site was not run. A targeted run of 49 programmatic, draw.io, server and kernel tests
passed; a local draw.io Desktop export produced a valid PNG from editable XML.

Manual dogfoods exercised ODT/ODS and draw.io; they do not establish reliability
for arbitrary documents, models or diagram photographs. The documented results
apply to this working tree, not to a published artifact.

One additional end-to-end test created a Python scenario in a project during one
session and ran that file via `execute_program` in a new session. It verified an
allowed tool call, a rejected call outside the allowlist and the applicable journal
entries. The provider supplied scripted actions; this was not a live-model dogfood.
