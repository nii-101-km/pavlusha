#!/usr/bin/env python3
"""Measure real XTest typing via the unchanged OCR textbox's normal form request.

The test-only HTTP proxy records bytes sent by the application; it never reads or
controls the DOM. All input uses GuiRuntime click/type_text. Original work is
mounted read-only, book state is ephemeral, and model inference is not started.
"""
import argparse
import hashlib
import http.client
import json
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pavlusha_agent.gui import GuiRuntime, validate_gui_action
from pavlusha_agent.sandbox import build_bwrap_command

BOOK_PATH = "/work/test-books/The Project Gutenberg eBook #47464_ The Theory of Spectra and Atomic Constitution. - 47464-pdf.pdf"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ocr-work", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("type-text-results/ocr"))
    args = parser.parse_args()
    args.ocr_work = args.ocr_work.resolve()
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    app = args.ocr_work / "app.py"
    digest = hashlib.sha256(app.read_bytes()).hexdigest()
    captured = []
    submitted = threading.Event()

    class Proxy(BaseHTTPRequestHandler):
        def do_GET(self):
            self.forward()

        def do_POST(self):
            self.forward()

        def forward(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            connection = http.client.HTTPConnection("127.0.0.1", 8001, timeout=30)
            try:
                connection.request(self.command, self.path, body=body,
                                   headers={"Content-Type": self.headers.get("Content-Type", "application/json")})
                response = connection.getresponse()
                data = response.read()
                if self.command == "POST" and self.path == "/api/books":
                    captured.append({"request": json.loads(body), "status": response.status, "response": json.loads(data)})
                    submitted.set()
                self.send_response(response.status)
                self.send_header("Content-Type", response.getheader("Content-Type", "application/octet-stream"))
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            finally:
                connection.close()

        def log_message(self, *_args):
            pass

    # Bind first: refuse an already occupied browser-facing port.
    proxy = ThreadingHTTPServer(("127.0.0.1", 8000), Proxy)
    command = "GLM_OCR_BOOKS_DIR=/tmp/books /work/.venv/bin/python -B /work/app.py --no-load --port 8001"
    argv = build_bwrap_command(args.ocr_work, command, network=True)
    argv[argv.index("--bind")] = "--ro-bind"
    report = {"server_command": command, "browser_url": "http://127.0.0.1:8000", "cases": []}
    thread = None
    with (args.output / "server.log").open("w") as log:
        server = subprocess.Popen(argv, stdout=log, stderr=log, start_new_session=True)
        try:
            deadline = time.monotonic() + 60
            while True:
                if server.poll() is not None:
                    raise RuntimeError("OCR server exited; see server.log")
                try:
                    with urllib.request.urlopen("http://127.0.0.1:8001/api/health", timeout=1) as response:
                        report["health"] = json.load(response)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("OCR server not ready")
                    time.sleep(.5)
            thread = threading.Thread(target=proxy.serve_forever, daemon=True)
            thread.start()
            cases = ["AbC_XyZ-123", "ABC_xyz: Test_42!", BOOK_PATH]
            for index, expected in enumerate(cases):
                submitted.clear()
                # Fresh session/page gives a genuinely empty textbox for each case.
                with tempfile.TemporaryDirectory() as work:
                    with GuiRuntime(Path(work), args.output / str(index), max_command_timeout=120, network_allowed=True) as gui:
                        result, _ = gui.start({"command": "pavlusha-browser http://127.0.0.1:8000", "network": True,
                                               "timeout": 120, "delay": 5})
                        assert result["state"] == "alive", result
                        result, _ = gui.execute("click", {"x": 200, "y": 225, "delay": .2})
                        assert "error" not in result, result
                        chunks = []
                        for offset in range(0, len(expected), 64):
                            chunk = expected[offset:offset + 64]
                            kind, data = validate_gui_action({"action": "type_text", "text": chunk, "delay": .2}, 120)
                            result, typed = gui.execute(kind, data)
                            assert typed is not None, result
                            chunks.append(chunk)
                        (args.output / f"{index}-typed.png").write_bytes(typed.clean_png)
                        result, after = gui.execute("click", {"x": 725, "y": 225, "delay": 2})
                        assert after is not None, result
                        assert submitted.wait(5), "OCR textbox did not submit"
                        (args.output / f"{index}-submitted.png").write_bytes(after.observation_png)
                        actual = captured[-1]
                        assert actual["request"]["source"] == expected, actual
                        if expected == BOOK_PATH:
                            assert actual["status"] == 201, actual
                            assert actual["response"]["source"] == expected, actual
                        else:
                            assert actual["status"] == 400, actual
                            assert actual["response"]["error"] == "source path does not exist: " + expected, actual
                        xserver, browser, socket_path = gui.display.server, gui.process, gui.display.socket_path
                        result, _ = gui.execute("gui_close", {"delay": .5})
                        assert result["state"] == "closed", result
                        assert browser.poll() is not None and xserver.poll() is not None and not socket_path.exists()
                        record = {"expected": expected, "actual": actual["request"]["source"], "chunks": chunks,
                                  "http_status": actual["status"], "response": actual["response"], "cleanup": "closed"}
                        report["cases"].append(record)
                        print(json.dumps({k: v for k, v in record.items() if k != "response"}, ensure_ascii=False), flush=True)
        finally:
            if thread is not None:
                proxy.shutdown()
                thread.join()
            proxy.server_close()
            if server.poll() is None:
                server.terminate()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()
    assert hashlib.sha256(app.read_bytes()).hexdigest() == digest
    report["app_sha256_unchanged"] = digest
    report["result"] = "PASS"
    (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print("PASS: actual OCR textbox requests match all three strings character for character")


if __name__ == "__main__":
    main()
