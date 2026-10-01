#!/usr/bin/env python3
"""Real OCR GUI smoke. No DOM automation; fixed pixels selected from the screenshot.

The supplied OCR work is mounted read-only. Model loading is skipped and book
state lives in the server sandbox's /tmp; neither app.py nor existing books change.
"""
import argparse
import io
import json
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image, ImageChops
from pavlusha_agent.gui import GuiRuntime
from pavlusha_agent.sandbox import build_bwrap_command


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ocr-work", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("smoke-results"))
    args = parser.parse_args()
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    # Refuse to accidentally test an unrelated pre-existing server.
    try:
        urllib.request.urlopen("http://127.0.0.1:8000", timeout=1)
    except OSError:
        pass
    else:
        raise RuntimeError("port 8000 already serves HTTP; stop that server before smoke")
    command = "GLM_OCR_BOOKS_DIR=/tmp/books /work/.venv/bin/python -B /work/app.py --no-load"
    argv = build_bwrap_command(args.ocr_work.resolve(), command, network=True)
    argv[argv.index("--bind")] = "--ro-bind"
    report = {"server_command": command, "ocr_work_read_only": str(args.ocr_work.resolve()), "actions": []}
    with (args.output / "server.log").open("w") as log:
        server = subprocess.Popen(argv, stdout=log, stderr=log, start_new_session=True)
        try:
            deadline = time.monotonic() + 60
            while True:
                if server.poll() is not None:
                    raise RuntimeError("OCR server exited; see server.log")
                try:
                    with urllib.request.urlopen("http://127.0.0.1:8000/api/health", timeout=1) as response:
                        report["health"] = json.load(response)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("OCR server did not become ready")
                    time.sleep(.5)
            with tempfile.TemporaryDirectory() as work:
                with GuiRuntime(Path(work), args.output, max_command_timeout=60, network_allowed=True) as gui:
                    start = {"command": "pavlusha-browser http://127.0.0.1:8000", "network": True, "timeout": 60, "delay": 8}
                    result, before = gui.start(start)
                    report["actions"].append({"action": "gui_start", **start, **result})
                    assert before is not None, result
                    (args.output / "before.png").write_bytes(before.clean_png)
                    socket_path, xserver, browser = gui.display.socket_path, gui.display.server, gui.process
                    result, after = gui.execute("click", {"x": 725, "y": 225, "delay": 2})
                    report["actions"].append(result)
                    assert after is not None, result
                    (args.output / "after.png").write_bytes(after.observation_png)
                    (args.output / "after-clean.png").write_bytes(after.clean_png)
                    assert "YOUR CLICK" in after.message()["content"][0]["text"]
                    # An actual HTTP callback and changed clean pixels, not the Core marker.
                    assert '"POST /api/books HTTP/1.1" 400' in (args.output / "server.log").read_text()
                    first = Image.open(io.BytesIO(before.clean_png)).convert("RGB").crop((40, 247, 400, 270))
                    last = Image.open(io.BytesIO(after.clean_png)).convert("RGB").crop((40, 247, 400, 270))
                    assert ImageChops.difference(first, last).getbbox(), "OCR status line did not change"
                    result, _ = gui.execute("gui_close", {"delay": .5})
                    report["actions"].append({k: v for k, v in result.items() if not k.endswith("_tail")})
                    assert result["state"] == "closed", result
                    assert gui.process is None and gui.display is None
                    assert browser.poll() is not None and xserver.poll() is not None
                    assert not socket_path.exists()
                    report["gui_cleanup"] = "browser and Xvfb exited; socket and runtime handles released"
        finally:
            if server.poll() is None:
                server.send_signal(signal.SIGTERM)
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()
            report["server_exit_code"] = server.returncode
    report["result"] = "PASS"
    (args.output / "smoke.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    print("Review before.png and after.png: Book OCR, Error: source is required, YOUR CLICK.")


if __name__ == "__main__":
    main()
