# Default GUI availability verification

GUI availability changed from opt-in to default-on. `--gui` remains compatible;
`--no-gui` disables it in both interactive and non-interactive launches.
The runtime checks existing GUI prerequisites before advertising actions. A
`GuiError` from that check produces a bounded `GUI unavailable` diagnostic and
omits GUI actions from the system prompt, response schema and action admission.
Shell/custom functions remain available. Xvfb still starts only on `gui_start`;
browser/viewer failures use the existing GUI fail path.

## Real vision smoke

Command run from the repository:

```bash
.venv/bin/python /tmp/gui-default-smoke.py
```

The local harness uses the ordinary CLI parser and `run_agent` path without
`--gui`, copies the existing laboratory `render/page-4.png` into an isolated
workdir, and prepares a committed completed-work checkpoint for the smoke.
It scripts only the first two Worker actions:

```json
{"action":"gui_start","command":"pavlusha-browser file:///work/viewer.html","network":false,"delay":8}
{"action":"view_gui","delay":1}
```

The HTML viewer displays the unchanged PNG at 740 px width. All GUI execution,
private Xvfb/bubblewrap/browser handling and screenshot transport are real.
The next Worker turn is an actual local LM Studio inference with
`qwen/qwen3.8-27b`, which the native backend reports as vision-capable.
The harness observes the outbound HTTP payload without replacing it.

Evidence is stored locally outside the repository:

- `/tmp/pavlusha-gui-default-smoke-3/report.json`: no GUI flag supplied,
  actual request model, screenshot byte count/hash, response and exit code 0.
- `/tmp/pavlusha-gui-default-smoke-3/state/gui-runtime/latest-clean.png`:
  800×600 screenshot, 49,103 bytes.
- `/tmp/gui-default-smoke.log`: successful runtime summary.

The actual `/chat/completions` request contained one `image_url` PNG screenshot
with SHA-256
`3a39fccf5978c288c9bf903359f1925dc8e0f4086f49bbe1216a5cae8f57d842`.
The model named the yellow “Задание 3” heading, rows 10–13 and 23–26 and the
visible НЕ/И/ИЛИ instructions. These match the captured screenshot. This proves
image delivery and useful interpretation for the visible viewport, not a full
document visual review or correctness of content below the viewport.
Source PNG SHA-256 remained
`c13b22883be8a99bb03d9cd2dbaaa8e077553689a3a859a86365a1aa1310ec53`.

## Tests and boundaries

- Targeted GUI/CLI/recovery/checkpoint/schema tests: 105 tests, OK; 4 skipped.
- Full suite: 375 tests, OK; 26 skipped.
- Compile/import sanity and `git diff --check` pass; change diff reviewed.

Regression coverage includes default/explicit GUI flags in both interactive
modes, post-checkpoint availability, default screenshot observations, and
missing Xvfb/Python GUI prerequisites with successful shell/function actions.
Older schema tests explicitly opt out when testing a contract without GUI.

Rendering creates files; GUI opens them; `view_gui` supplies a transient
screenshot; an image-capable model interprets it. Unsupported image-input
provider errors retain their existing path. No automatic installation,
alternate vision model, OCR fallback or `view_image` action was introduced.
GUI implementation, image transport, Office packs and Core/State/checkpoint/
recovery semantics were not redesigned. Only CLI default and runtime
availability/degradation handling changed. No changes were committed.
