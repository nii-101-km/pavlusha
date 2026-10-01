# Private GUI tools

Publication note: local run logs, state snapshots, screenshots and result directories
referenced in this historical report are excluded from the source distribution.
Required regression fixtures remain in `tests/fixtures/`; OCR application smoke tools
require a separately supplied OCR work directory.

The GUI path is an optional deterministic device layer for the existing Pavlusha Worker. It was
adapted from the proven private-X11 mechanics in `nii-runtime-debugger` (`live_gui.py` and
`gui_helper.py`). The old NII model/controller, Reviewer, history, action budgets and semantic GUI
logic are deliberately not imported.

## Boundary

`--gui` creates one controller-owned 800x600 Xvfb display. The GUI application itself is launched
inside Pavlusha's existing bubblewrap `/work` sandbox. Only that private X11 socket and its temporary
Xauthority cookie are added to the sandbox. The host desktop is never selected as `DISPLAY`.

The controller owns Xvfb, XTest input, screenshots and annotation. The Worker owns only the public
JSON actions. GUI observations are current-world evidence: image bytes are transient and are not
written into Recent History, Project State, checkpoint handoff, or recovery generations. Diagnostic
clean/annotated PNG copies under the state directory are not recovery authority.

A cold restart intentionally does not reconstruct a GUI session. Durable recovery remains Project
State + handoff; `/work` remains current-world truth. The Worker may start the GUI application again
and inspect its current files.

## Installation and launch

Core and GUI Python dependencies are included in `requirements.txt`:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
sudo apt-get install xvfb epiphany-browser dbus-daemon libglib2.0-bin
```

The controller host needs `Xvfb`. `--gui` fails explicitly if Xvfb, `python-xlib`, or Pillow is
unavailable. Python GUI dependencies stay in the controller `.venv`, separate from the OCR app `.venv`.

For web interfaces Core exposes **`pavlusha-browser URL`** only in the GUI sandbox PATH.
It uses fixed executables `/usr/bin/epiphany`, `/usr/bin/dbus-run-session`,
`/usr/bin/dbus-daemon`, and `/usr/bin/gsettings`. A missing executable yields exit 127
with the required host packages; no browser discovery or fallback occurs. Missing GSettings
schemas also fail at launch. Ubuntu snap Firefox and `/lib/chatgpt` are not used.

```json
{"action":"gui_start","command":"pavlusha-browser http://127.0.0.1:8000","network":true,"timeout":300,"delay":8}
```

A web server started in a separate shell sandbox needs `network:true` and controller `--network`
so both share host loopback. With `network:false` the GUI has its own loopback: start the server
and browser in the same `gui_start` command if network sharing is not granted.

The browser helper starts a **private D-Bus session inside bubblewrap**, uses software GTK/WebKit
rendering, and keeps its browser profile/settings/runtime directory in sandbox `/tmp`.
It suppresses only the first-run default-browser modal via session-local GSettings keyfile storage.
It does not mount a host session bus, desktop socket, GPU device, or share host IPC.
WebKit's nested sandbox is disabled with `WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS=1` because this
host rejects its namespace creation; **Pavlusha's existing outer bubblewrap still isolates the browser
and all children**. The browser consequently has the same `/work` access/network authority as other
Worker applications. These browser-specific settings do not affect ordinary shell or native GUI commands.

Enable the tools with the ordinary agent command plus `--gui`:

```bash
.venv/bin/python agent.py --gui ... "task"
```

`gui_start.timeout` remains accepted/validated for compatibility but no longer limits session
lifetime. The same private session persists across model turns, idle time, shell actions and
periodic/HIGH review until `gui_close` or controller termination (including explicit overflow cold
recovery). Keep the main GUI command in the foreground. A server launched in the same `gui_start`
command shares and belongs to that sandbox; it is cleaned up with the browser. GUI does not extend
ordinary shell-action process lifetime. `delay` is only a bounded settle wait; Xvfb startup and
individual XTest/screenshot helpers still have their separate short deadlines.
Network remains separately authorized: `gui_start` may use `network:true` only when the
controller itself was started with `--network`.

## Worker contract

The minimal first transplant exposes:

```json
{"action":"gui_start","command":"python app.py","network":false,"timeout":300,"delay":0.8}
{"action":"view_gui","delay":3.0}
{"action":"click","x":400,"y":300,"delay":0.5}
{"action":"right_click","x":400,"y":300,"delay":0.5}
{"action":"drag","x1":100,"y1":100,"x2":500,"y2":300,"delay":0.5}
{"action":"type_text","text":"hello","delay":0.5}
{"action":"gui_close","delay":0.5}
```

Coordinates are integer pixels inside 800x600. `delay` is a bounded 0..10 second post-action wait.
`view_gui(delay=N)` is also the primitive for simply waiting and observing again; no semantic
`wait_for_button`/`wait_for_page` layer exists.

Each successful input action follows the same mechanical sequence:

1. execute the requested input on the private display;
2. wait the Worker-selected `delay`;
3. capture a fresh clean screenshot;
4. make an observation copy;
5. overlay only the immediately preceding mouse gesture, when applicable;
6. send that one current image to the Worker on the next request.

`gui_close` asks clients to close, then always releases the GUI sandbox, log handles, Xvfb and
authority cookie; its result is `state:closed`. If the application exits before capture, no screenshot
is returned. Repeated close also cleans up an already-exited session. Failed starts release their display.

A left click is marked `YOUR CLICK`, a right click `RIGHT CLICK`, and a drag has an arrow plus `DRAG`.
The system prompt and the image-adjacent text explicitly state that these marks are Core annotations,
not application UI. `view_gui` returns a clean frame and clears the previous gesture marker. The
underlying clean PNG is never painted over.

The marker is proprioceptive, not semantic: it means "Core executed your previous mouse gesture at
these coordinates." It does not claim which widget was hit or whether the intended action succeeded.
The Worker compares the marker and visible consequence, then corrects coordinates itself.

## Lifecycle invariants preserved

GUI work uses the same Project State gates as shell work. A true HIGH checkpoint still blocks normal
work until `project_review_complete`; the periodic questionnaire remains distinct. GUI actions do not
increment the shell-operation counter, so `--project-review-every` retains its established meaning.
The GUI layer does not publish checkpoint generations, modify handoff, change Worker-selected
evidence semantics, add semantic memory, or participate in reasoning-loop recovery.

Only one current screenshot is present in a Worker request. It replaces the previous GUI image rather
than accumulating in chronological history. Prompt guarding uses a fixed conservative image allowance
instead of counting base64 bytes as text; provider-reported prompt usage remains authoritative on
following turns.

## Root cause and verification (2026-09-30, Ubuntu 24.04)

Before repair, real `gui_start` reproduced:

- `firefox URL`: exit 1, Ubuntu snap stub requires Firefox snap.
- `epiphany --private-instance URL`: exit 134, libportal cannot create XdpPortal against the
  intentionally unavailable D-Bus address.
- Adding only `dbus-run-session`: exit 133, WebKit cannot create its nested bwrap namespace/dbus-proxy.
- Disabling the nested WebKit sandbox: process alive but black screenshot; Mesa reports X11 shared
  memory/DRI failures. Software GTK/WebKit rendering gives the actual window.
- The first-run default-browser modal remained mapped behind the main window without a window
  manager and blocked input. Session-local `ask-for-default=false` allows coordinate clicks to reach UI.

The repair is a bound browser helper plus explicit Worker instructions and deterministic GUI cleanup.
No semantic targets, DOM driver, new agent layer, Project State or shell policy changes were added.

Run the reproducible real OCR smoke from the runtime directory:

```bash
.venv/bin/python tools/smoke_gui_ocr.py \
  --ocr-work /path/to/ocr-work \
  --output smoke-results
PAVLUSHA_TEST_GUI_BROWSER=1 .venv/bin/python -m unittest discover -s tests
```

The smoke starts the unchanged real OCR app with `--no-load` in a separate bubblewrap (original work
mounted read-only; books in sandbox `/tmp`). It opens `http://127.0.0.1:8000` through `pavlusha-browser`,
captures the visible Book OCR interface, then clicks Open at `(725,225)` through the ordinary Core
XTest action. The empty source produces a real `POST /api/books` -> 400 and visible
`Error: source is required`, with `YOUR CLICK` on the observation copy. This deliberately tests
GUI transport and UI callback, not model inference. It verifies browser/Xvfb exit and socket cleanup.
Review `before.png`, `after.png`, `after-clean.png`, `server.log`, and `smoke.json` in the output directory.

Opt-in real browser regression tests check software rendering, a coordinate click's actual HTTP callback
and changed clean screen pixels, cleanup, and explicit missing-browser failure. Ordinary unit tests also
check the helper is absent from shell sandbox PATH and cleanup after the application already exited.

Verified here: real OCR smoke PASS; full suite `Ran 180 tests`, `OK (skipped=2)` (the two live-network
tests require `PAVLUSHA_LIVE_NETWORK=1`). Both real browser regressions ran, not skipped. Compilation and
`agent.py --help` also exited 0. Existing dependencies on this machine were sufficient; no system
package installation or host policy change was necessary.

Double-click, scrolling, arbitrary hotkeys and semantic target detection remain outside this repair.

## Literal text / XKB regression

`type_text` types ordinary printable text through XTest, not a clipboard or a path-specific mechanism.
Its existing limit is 64 characters per action; longer text is sent in consecutive chunks while the
same textbox retains focus. No casing or punctuation conversion is intended.

On this Xvfb, assigning the temporary keycode a single uppercase keysym `A` makes XKB normalize
its levels to `a/A`. The unshifted XTest press therefore typed `a`. The helper now assigns the same
literal keysym to both levels (`A/A`) and restores the original mapping in its existing `finally`.
Independent real-browser forms reproduced `AbC_XyZ-123` -> `abc_xyz-123` before the change;
Shift punctuation already worked. Regression coverage submits actual textbox contents for mixed
case, Shift punctuation, accented/Cyrillic/Greek letters and the complete original PDF path, and
checks restoration of the temporary key mapping after each action.

The real OCR typing smoke is:

```bash
.venv/bin/python tools/smoke_gui_type_text.py \
  --ocr-work /path/to/ocr-work \
  --output type-text-results/ocr
```

It starts the unchanged OCR app on port 8001 and a test-only transparent HTTP recorder on port
8000. The recorder observes the application's normal form requests and forwards them unchanged;
it never reads or manipulates the DOM. Every character enters through Core's XTest `type_text`.
The two short strings must match the submitted `source` exactly; their expected missing-file
responses also echo the same string. The full original path must match both the submitted value
and the created book's `source` (HTTP 201). Original work is mounted read-only, book state lives in
sandbox `/tmp`, and model inference is not started. Screenshots and the exact values are saved
in the output directory. No OCR app or browser runtime change is needed for this keyboard repair.

After this repair: actual OCR textbox smoke PASS for all three required strings; the complete PDF
path created a 209-page book with HTTP 201 and exactly matching `source`. The pre-existing OCR
click/marker/cleanup smoke also passed. Full suite with `PAVLUSHA_TEST_GUI_BROWSER=1`: `Ran 181 tests`,
`OK (skipped=2)` (the same optional live-network tests). Compilation and `agent.py --help` exited 0.
