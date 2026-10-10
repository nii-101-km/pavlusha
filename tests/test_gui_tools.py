import io
import math
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from pavlusha_agent.gui import (
    GuiError,
    GuiObservation,
    GuiRuntime,
    PrivateDisplay,
    annotate_png,
    validate_gui_action,
)
from pavlusha_agent.gui_helper import double_click as helper_double_click, click as helper_click, drag as helper_drag, right_click as helper_right_click


def png_fixture() -> bytes:
    image = Image.new("RGB", (800, 600), "white")
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


class GuiValidationTests(unittest.TestCase):
    def test_cli_gui_default_and_explicit_modes(self):
        from pavlusha_agent.cli import build_parser
        for interactive in ([], ['--interactive']):
            for flags, expected in (([], True), (['--gui'], True), (['--no-gui'], False)):
                with self.subTest(interactive=interactive, flags=flags):
                    self.assertEqual(build_parser().parse_args([*interactive, *flags, 'task']).gui, expected)

    def test_public_shapes_and_delay_defaults(self):
        kind, data = validate_gui_action({"action": "click", "x": 10, "y": 20})
        self.assertEqual(kind, "click")
        self.assertEqual(data["delay"], 0.5)
        kind, data = validate_gui_action({"action": "view_gui", "delay": 3})
        self.assertEqual(kind, "view_gui")
        self.assertEqual(data["delay"], 3.0)
        kind, data = validate_gui_action(
            {"action": "gui_start", "command": "python app.py"}
        )
        self.assertEqual(kind, "gui_start")
        self.assertNotIn("timeout", data)
        self.assertFalse(data["network"])

    def test_bounds_and_exact_fields(self):
        bad = [
            {"action": "gui_start", "command": "app", "timeout": 30},
            {"action": "click", "x": 800, "y": 1},
            {"action": "click", "x": True, "y": 1},
            {"action": "right_click", "x": 1, "y": 600},
            {"action": "drag", "x1": 0, "y1": 0, "x2": -1, "y2": 1},
            {"action": "click", "x": 1, "y": 2, "delay": 10.1},
            {"action": "view_gui", "delay": float("nan")},
            {"action": "type_text", "text": "x" * 65},
            {"action": "type_text", "text": "a\nb"},
            {"action": "click", "x": 1, "y": 2, "button": 1},
        ]
        for action in bad:
            with self.subTest(action=action):
                with self.assertRaises(GuiError):
                    validate_gui_action(action)


class AnnotationTests(unittest.TestCase):
    def test_click_annotation_is_observation_only(self):
        clean = png_fixture()
        marked = annotate_png(clean, {"action": "click", "x": 400, "y": 300})
        self.assertNotEqual(clean, marked)
        self.assertEqual(Image.open(io.BytesIO(marked)).size, (800, 600))
        observation = GuiObservation(clean, marked, {"action": "click", "x": 400, "y": 300}, time.time())
        message = observation.message()
        self.assertIn("YOUR CLICK", message["content"][0]["text"])
        self.assertTrue(message["content"][1]["image_url"]["url"].startswith("data:image/png;base64,"))
        # The clean evidence itself is untouched.
        self.assertEqual(clean, observation.clean_png)

    def test_drag_annotation_and_clean_view(self):
        clean = png_fixture()
        marked = annotate_png(clean, {"action": "drag", "x1": 10, "y1": 20, "x2": 700, "y2": 500})
        self.assertNotEqual(clean, marked)
        msg = GuiObservation(clean, marked, {"action": "drag", "x1": 10, "y1": 20, "x2": 700, "y2": 500}, time.time()).message()
        self.assertIn("DRAG", msg["content"][0]["text"])
        clean_msg = GuiObservation(clean, clean, None, time.time()).message()
        self.assertIn("No previous mouse-action marker", clean_msg["content"][0]["text"])


class FakeProcess:
    def poll(self):
        return None

    def wait(self, timeout=None):
        raise subprocess.TimeoutExpired("fake", timeout)


class FakeDisplay:
    def __init__(self):
        self.calls = []

    def action(self, action, timeout):
        self.calls.append(dict(action))
        if action["action"] == "screenshot":
            return png_fixture()
        return b""


class GuiRuntimeTests(unittest.TestCase):
    def test_post_action_capture_has_only_latest_gesture(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = GuiRuntime(root / "work", root / "state", network_allowed=False)
            runtime.process = FakeProcess()
            runtime.display = FakeDisplay()
            result, obs = runtime.execute("click", {"x": 12, "y": 34, "delay": 0.0})
            self.assertEqual(result["state"], "alive")
            self.assertEqual(obs.gesture, {"action": "click", "x": 12, "y": 34})
            self.assertEqual([c["action"] for c in runtime.display.calls], ["click", "screenshot"])
            result, obs = runtime.execute("view_gui", {"delay": 0.0})
            self.assertEqual(result["state"], "alive")
            self.assertIsNone(obs.gesture)
            self.assertEqual(runtime.display.calls[-1]["action"], "screenshot")
            # Do not call close(): FakeProcess intentionally has no pid; clear first.
            runtime.process = None
            runtime.display = None

    def test_gui_start_failure_is_returned_to_worker_not_raised(self):
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = GuiRuntime(root / "work", root / "state", network_allowed=False)
            (root / "work").mkdir(exist_ok=True)
            data = {"command": "python app.py", "network": False, "delay": 0.0}
            with patch("pavlusha_agent.gui.PrivateDisplay.__enter__", side_effect=GuiError("xvfb failed")):
                result, observation = runtime.start(data)
            self.assertEqual(result["state"], "not_started")
            self.assertIn("xvfb failed", result["error"])
            self.assertIsNone(observation)
            self.assertIsNone(runtime.process)
            self.assertIsNone(runtime.display)

    def test_gui_bwrap_reuses_existing_sandbox_and_adds_only_private_display(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = root / "work"
            work.mkdir()
            runtime = GuiRuntime(work, root / "state", network_allowed=False)
            auth = root / "auth"
            auth.write_text("x")
            runtime.display = SimpleNamespace(
                socket_path=Path("/tmp/.X11-unix/X4321"), number=4321, auth=auth
            )
            argv = runtime._bwrap_argv("python app.py", network=False)
            joined = " ".join(str(x) for x in argv)
            self.assertIn("--bind", argv)
            self.assertIn(str(work), argv)
            self.assertIn("/work", argv)
            self.assertIn("/tmp/.X11-unix/X4321", joined)
            self.assertIn("DISPLAY", argv)
            self.assertIn(":4321", argv)
            self.assertIn("XAUTHORITY", argv)
            self.assertNotIn("/home", argv)
            self.assertIn("/tmp/.pavlusha-gui-bin/pavlusha-browser", argv)
            self.assertIn("PAVLUSHA_PRIVATE_GUI", argv)
            from pavlusha_agent.sandbox import build_bwrap_command
            self.assertNotIn("/tmp/.pavlusha-gui-bin/pavlusha-browser",
                             build_bwrap_command(work, "true", network=False))
            runtime.display = None

    def test_gui_close_releases_display_even_if_application_already_exited(self):
        from unittest.mock import Mock, MagicMock
        with tempfile.TemporaryDirectory() as tmp:
            runtime = GuiRuntime(Path(tmp), Path(tmp), network_allowed=False)
            display = MagicMock()
            runtime.display = display
            runtime.process = Mock(poll=Mock(return_value=0))
            result, obs = runtime.execute("gui_close", {"delay": 0})
            self.assertEqual(result["state"], "closed")
            self.assertIsNone(obs)
            display.__exit__.assert_called_once()
            self.assertIsNone(runtime.display)
            self.assertIsNone(runtime.process)


@unittest.skipUnless(os.environ.get("PAVLUSHA_TEST_GUI_BROWSER") == "1", "opt-in real bwrap browser test")
class BrowserSandboxRegressionTests(unittest.TestCase):
    def test_type_text_preserves_actual_form_value_and_restores_keymap(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from urllib.parse import parse_qs
        from unittest.mock import patch
        from Xlib import display

        values = []
        submitted = threading.Event()
        loaded = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.page()
                loaded.set()

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"])).decode()
                values.append(parse_qs(body, keep_blank_values=True)["text"][0])
                submitted.set()
                self.page()

            def page(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                # Ordinary form submission is the oracle: no DOM inspection/automation.
                self.wfile.write(b'''<form method="post">
                    <input name="text" style="position:absolute;left:40px;top:40px;width:700px;height:48px">
                    <button style="position:absolute;left:40px;top:120px;width:200px;height:40px">Submit</button>
                    </form>''')

            def log_message(self, *_args):
                pass

        cases = [
            "AbC_XyZ-123", "ABC_xyz: Test_42!", '!@#$%^&*()_+{}:"<>?~|', "Éé Яя Ωω",
            "/work/test-books/The Project Gutenberg eBook #47464_ The Theory of Spectra and Atomic Constitution. - 47464-pdf.pdf",
        ]
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                with GuiRuntime(Path(tmp), Path(tmp) / "state", network_allowed=True) as runtime:
                    result, _ = runtime.start({"command": f"pavlusha-browser http://127.0.0.1:{server.server_port}",
                                              "network": True, "delay": 5})
                    self.assertEqual(result["state"], "alive", result)
                    self.assertTrue(loaded.wait(5), "browser did not load test form")
                    runtime.execute("view_gui", {"delay": 1})
                    with patch.dict(os.environ, {"XAUTHORITY": str(runtime.display.auth)}):
                        keyboard = display.Display(runtime.display.environment["DISPLAY"])
                    try:
                        code = keyboard.display.info.max_keycode
                        original = keyboard.get_keyboard_mapping(code, 1)
                        for expected in cases:
                            with self.subTest(expected=expected):
                                submitted.clear()
                                runtime.execute("click", {"x": 200, "y": 120, "delay": .2})
                                # Preserve the existing public 64-character action limit.
                                for offset in range(0, len(expected), 64):
                                    kind, data = validate_gui_action({"action": "type_text", "text": expected[offset:offset + 64],
                                                                     "delay": .2})
                                    result, obs = runtime.execute(kind, data)
                                    self.assertEqual(result["state"], "alive", result)
                                    self.assertIsNotNone(obs, result)
                                    self.assertEqual(keyboard.get_keyboard_mapping(code, 1), original)
                                result, _ = runtime.execute("click", {"x": 140, "y": 195, "delay": 1})
                                if not submitted.wait(1):
                                    diagnostic = Path("/tmp/pavlusha-type-text-failure.png")
                                    diagnostic.write_bytes(runtime.latest.clean_png)
                                    self.fail(f"textbox did not submit; screenshot: {diagnostic}; {runtime._tails()}")
                                self.assertEqual(values[-1], expected)
                    finally:
                        keyboard.close()
                    result, _ = runtime.execute("gui_close", {"delay": .5})
                    self.assertEqual(result["state"], "closed", result)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_browser_renders_delivers_coordinate_click_and_closes(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        received = threading.Event()
        doubled = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/clicked":
                    received.set()
                if self.path == "/doubled":
                    doubled.set()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b'''<body style="background:#ff0000">
                    <button style="position:absolute;left:50px;top:50px;width:200px;height:100px"
                    ondblclick="fetch('/doubled')"
                    onclick="document.body.style.background='#00ff00';fetch('/clicked')">Click</button>''')

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                with GuiRuntime(Path(tmp), Path(tmp) / "state", network_allowed=True) as runtime:
                    result, before = runtime.start({
                        "command": f"pavlusha-browser http://127.0.0.1:{server.server_port}",
                        "network": True, "delay": 5,
                    })
                    self.assertEqual(result["state"], "alive", result)
                    self.assertEqual(Image.open(io.BytesIO(before.clean_png)).getpixel((400, 400)), (255, 0, 0))
                    socket_path = runtime.display.socket_path
                    xserver = runtime.display.server
                    process = runtime.process
                    result, after = runtime.execute("click", {"x": 100, "y": 150, "delay": 1})
                    self.assertTrue(received.wait(1), "coordinate click did not reach browser UI")
                    self.assertEqual(Image.open(io.BytesIO(after.clean_png)).getpixel((400, 400)), (0, 255, 0))
                    self.assertIn("YOUR CLICK", after.message()["content"][0]["text"])
                    self.assertNotEqual(after.clean_png, after.observation_png)
                    result, double_obs = runtime.execute("double_click", {"x": 100, "y": 150, "delay": 1})
                    self.assertTrue(doubled.wait(1), "double click did not deliver dblclick")
                    self.assertIn("DOUBLE CLICK", double_obs.message()["content"][0]["text"])
                    result, _ = runtime.execute("gui_close", {"delay": .5})
                    self.assertEqual(result["state"], "closed", result)
                    self.assertIsNotNone(process.poll())
                    self.assertIsNotNone(xserver.poll())
                    self.assertFalse(socket_path.exists())
                    self.assertIsNone(runtime.display)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_missing_browser_is_explicit_and_leaves_no_display(self):
        from unittest.mock import patch
        original = GuiRuntime._bwrap_argv
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing = root / "not-executable"
            missing.touch(mode=0o600)

            def masked(runtime, command, *, network):
                argv = original(runtime, command, network=network)
                at = argv.index("/bin/bash")
                argv[at:at] = ["--ro-bind", str(missing), "/usr/bin/epiphany"]
                return argv

            with GuiRuntime(root, root / "state", network_allowed=False) as runtime:
                with patch.object(GuiRuntime, "_bwrap_argv", masked):
                    result, obs = runtime.start({"command": "pavlusha-browser http://127.0.0.1:8000",
                                                 "network": False, "delay": 2})
                self.assertEqual(result["exit_code"], 127, result)
                self.assertIn("missing /usr/bin/epiphany", result["stderr_tail"])
                self.assertIn("Do not search", result["stderr_tail"])
                self.assertIsNone(obs)
                self.assertIsNone(runtime.display)


@unittest.skipUnless(shutil.which("Xvfb"), "Xvfb unavailable")
class PrivateDisplaySmokeTests(unittest.TestCase):
    def test_real_private_display_capture_and_mouse_gestures(self):
        # This exercises the transplanted Xvfb + XTest + Pillow path without touching host desktop.
        with tempfile.TemporaryDirectory() as tmp:
            with PrivateDisplay(Path(tmp)) as display:
                before = display.action({"action": "screenshot"}, 2.0)
                self.assertTrue(before.startswith(b"\x89PNG\r\n\x1a\n"))
                display.action({"action": "click", "x": 100, "y": 100}, 2.0)
                display.action({"action": "right_click", "x": 120, "y": 120}, 2.0)
                display.action({"action": "drag", "x1": 10, "y1": 10, "x2": 30, "y2": 30}, 2.0)


class HelperReleaseTests(unittest.TestCase):
    def test_double_click_sends_two_presses_and_releases(self):
        events = []
        X = SimpleNamespace(MotionNotify=1, ButtonPress=2, ButtonRelease=3)
        d = SimpleNamespace(sync=lambda: None)
        xtest = SimpleNamespace(fake_input=lambda _d, kind, *args, **kw: events.append(kind))
        with patch("pavlusha_agent.gui_helper.time.sleep") as sleep:
            helper_double_click(d, X, xtest, {"x": 5, "y": 6})
        self.assertEqual(events, [1, 2, 3, 1, 2, 3])
        sleep.assert_called_once_with(.05)
        self.assertEqual(validate_gui_action({"action": "double_click", "x": 5, "y": 6})[0],
                         "double_click")

    def test_left_click_releases_after_sync_failure(self):
        events = []
        X = SimpleNamespace(MotionNotify=1, ButtonPress=2, ButtonRelease=3)

        def fake_input(_d, kind, *args, **kwargs):
            events.append((kind, args, kwargs))

        def sync():
            if events and events[-1][0] == 2:
                raise OSError("sync failed after press")

        d = SimpleNamespace(sync=sync)
        with self.assertRaises(OSError):
            helper_click(d, X, SimpleNamespace(fake_input=fake_input), {"x": 5, "y": 6})
        self.assertEqual(events[-1][0], 3)

    def test_right_click_releases_after_sync_failure(self):
        events = []
        X = SimpleNamespace(MotionNotify=1, ButtonPress=2, ButtonRelease=3)

        def fake_input(_d, kind, *args, **kwargs):
            events.append((kind, args, kwargs))

        def sync():
            if events and events[-1][0] == 2:
                raise OSError("sync failed after press")

        d = SimpleNamespace(sync=sync)
        with self.assertRaises(OSError):
            helper_right_click(d, X, SimpleNamespace(fake_input=fake_input), {"x": 5, "y": 6})
        self.assertEqual(events[-1][0], 3)

    def test_drag_releases_after_motion_failure(self):
        events = []
        X = SimpleNamespace(MotionNotify=1, ButtonPress=2, ButtonRelease=3)

        def fake_input(_d, kind, *args, **kwargs):
            events.append((kind, args, kwargs))

        sync_count = 0

        def sync():
            nonlocal sync_count
            sync_count += 1
            if sync_count == 3:
                raise OSError("motion failure")

        d = SimpleNamespace(sync=sync)
        action = {"x1": 1, "y1": 2, "x2": 10, "y2": 20}
        with self.assertRaises(OSError):
            helper_drag(d, X, SimpleNamespace(fake_input=fake_input), action, time.monotonic() + 2)
        self.assertEqual(events[-1][0], 3)


class GuiPromptBudgetTests(unittest.TestCase):
    def test_base64_image_is_not_charged_as_text_bytes(self):
        from tests.test_context_preflight import prompt_size_bound

        huge = "A" * 1_000_000
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": "frame"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + huge}},
            ],
        }]
        bound = prompt_size_bound(messages)
        self.assertGreaterEqual(bound, 8192)
        self.assertLess(bound, 50_000)


class RuntimeGuiIntegrationTests(unittest.TestCase):
    def test_default_and_opt_out_contract_survive_checkpoint(self):
        import json
        from unittest.mock import patch
        from tests.test_checkpoint_snapshots import CheckpointSnapshotTests, done
        from tests.test_reasoning_window import init_turn, turn
        for flags, enabled in (([], True), (['--gui'], True), (['--no-gui'], False)):
            with self.subTest(flags=flags), patch('pavlusha_agent.gui.ensure_gui_dependencies'):
                helper = CheckpointSnapshotTests()
                seen, _, _ = helper.run_case([
                    init_turn(), turn({'action':'shell','command':'verify'}),
                    turn({'action':'project_review_complete','handoff':'continue'}),
                    done('verified'), turn({'action':'finish','summary':'done'}),
                ], extra=[*flags, '--history-high','2'])
                for index in (1, 3):
                    contract = json.dumps(helper.request_formats[index])
                    self.assertEqual('gui_start' in contract, enabled)
                    self.assertEqual('view_gui' in contract, enabled)
                self.assertEqual('"action":"gui_start"' in seen[1][0]['content'], enabled)

    def test_missing_dependencies_preserve_shell_and_functions(self):
        from unittest.mock import patch
        from tests.test_runtime_lifecycle import run_script
        from tests.test_reasoning_window import init_turn, turn
        from tests.test_checkpoint_snapshots import done
        for error in ('--gui requires Xvfb on the controller host',
                      '--gui requires python-xlib and Pillow in the controller environment'):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                module = root / 'functions.py'
                module.write_text('def value() -> int:\n    return 42\n')
                with patch('pavlusha_agent.gui.ensure_gui_dependencies', side_effect=GuiError(error)):
                    seen, result, failure = run_script(root, [init_turn(),
                        turn({'action':'call_function','name':'value','arguments':{}}),
                        turn({'action':'shell','command':'verify'}), done('verified'),
                        turn({'action':'finish','summary':'done'})], extra=['--functions',str(module)])
                self.assertEqual(result, 0, failure)
                self.assertNotIn('"action":"gui_start"', seen[1][0]['content'])
                self.assertIn('42', str(seen[2]))
                self.assertIn('SHELL RESULT', str(seen[3]))

    def test_worker_sees_one_fresh_transient_image_and_gesture_label(self):
        import copy
        import json
        from contextlib import redirect_stdout, redirect_stderr
        from unittest.mock import patch

        from pavlusha_agent.cli import build_parser
        from pavlusha_agent.core import ProviderTurn
        from pavlusha_agent.runtime import run_agent
        from pavlusha_agent.state_store import StateStore
        from tests.test_reasoning_window import init_turn, turn

        clean = png_fixture()

        class FakeGuiRuntime:
            instances = []

            def __init__(self, *args, **kwargs):
                self.closed = False
                self.calls = []
                FakeGuiRuntime.instances.append(self)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.closed = True

            def execute(self, kind, data):
                self.calls.append((kind, dict(data)))
                gesture = None
                if kind == "click":
                    gesture = {"action": "click", "x": data["x"], "y": data["y"]}
                marked = annotate_png(clean, gesture)
                obs = GuiObservation(clean, marked, gesture, time.time())
                return {"action": kind, "state": "alive", "observation": "fresh screenshot attached"}, obs

        replies = iter([
            init_turn(),
            turn({"action": "gui_start", "command": "python app.py", "delay": 0}),
            turn({"action": "click", "x": 100, "y": 120, "delay": 0}),
            turn({"action": "view_gui", "delay": 0}),
            turn({"action": "project_update", "changes": [
                {"op": "update_work", "id": "W001", "status": "DONE", "evidence": ["FILES: /work/app.py"]},
                {"op": "update_work", "id": "W002", "status": "DONE", "evidence": ["FILES: /work/app.py"]},
            ]}),
            turn({"action": "finish", "summary": "done"}),
        ])
        seen = []

        def worker(_provider, messages, **_kwargs):
            seen.append(copy.deepcopy(messages))
            return next(replies)

        def visual_messages(messages):
            return [m for m in messages if isinstance(m.get("content"), list)]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = build_parser().parse_args(['--no-interactive', '--no-live', '--no-network', '--project-map', 'off',
                "--workdir", str(root / "work"), "--state-dir", str(root / "state"),
                "--model", "fake", "--worker-context-budget", "40000", "--max-tokens", "1024",
                "--project-review-every", "0", "--max-steps", "6", "task",
            ])
            with patch("pavlusha_agent.runtime.shutil.which", return_value="/fake/bwrap"), \
                 patch("pavlusha_agent.runtime.GuiRuntime", FakeGuiRuntime), \
                 patch("pavlusha_agent.provider.ChatProvider.worker_completion", worker), \
                 redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(run_agent(args), 0)

            self.assertEqual(len(visual_messages(seen[0])), 0)
            self.assertEqual(len(visual_messages(seen[2])), 1)  # fresh frame after gui_start
            self.assertIn("No previous mouse-action marker", visual_messages(seen[2])[0]["content"][0]["text"])
            self.assertEqual(len(visual_messages(seen[3])), 1)  # old frame replaced, not accumulated
            self.assertIn("YOUR CLICK", visual_messages(seen[3])[0]["content"][0]["text"])
            self.assertEqual(len(visual_messages(seen[4])), 1)
            self.assertIn("No previous mouse-action marker", visual_messages(seen[4])[0]["content"][0]["text"])
            self.assertIn("GUI RESULT", str(seen[3]))
            self.assertTrue(FakeGuiRuntime.instances[0].closed)
            state = StateStore(root / "state", "task").load()
            self.assertEqual(state["counters"]["operation"], 0)  # GUI does not redefine shell-operation review cadence.

    def test_malformed_gui_action_gets_gui_specific_repair_message(self):
        import copy
        from contextlib import redirect_stdout, redirect_stderr
        from unittest.mock import patch

        from pavlusha_agent.cli import build_parser
        from pavlusha_agent.runtime import run_agent
        from tests.test_reasoning_window import init_turn, turn

        clean = png_fixture()

        class FakeGuiRuntime:
            instances = []

            def __init__(self, *args, **kwargs):
                self.calls = []
                FakeGuiRuntime.instances.append(self)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def execute(self, kind, data):
                self.calls.append((kind, dict(data)))
                obs = GuiObservation(clean, clean, None, time.time())
                return {"action": kind, "state": "alive", "observation": "fresh screenshot attached"}, obs

        replies = iter([
            init_turn(),
            turn({"tool": "gui_start", "command": "python app.py", "delay": 0}),
            turn({"action": "gui_start", "command": "python app.py", "delay": 0}),
            turn({"action": "project_update", "changes": [
                {"op": "update_work", "id": "W001", "status": "DONE"},
                {"op": "update_work", "id": "W002", "status": "DONE"},
            ]}),
            turn({"action": "finish", "summary": "done"}),
        ])
        seen = []

        def worker(_provider, messages, **_kwargs):
            seen.append(copy.deepcopy(messages))
            return next(replies)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = build_parser().parse_args(['--no-interactive', '--no-live', '--no-network', '--project-map', 'off',
                "--workdir", str(root / "work"), "--state-dir", str(root / "state"),
                "--model", "fake", "--worker-context-budget", "40000", "--max-tokens", "1024",
                "--project-review-every", "0", "--max-steps", "5", "--gui", "task",
            ])
            with patch("pavlusha_agent.runtime.shutil.which", return_value="/fake/bwrap"), \
                 patch("pavlusha_agent.runtime.GuiRuntime", FakeGuiRuntime), \
                 patch("pavlusha_agent.provider.ChatProvider.worker_completion", worker), \
                 redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(run_agent(args), 0)

        repair_prompt = str(seen[2])
        self.assertIn("unknown action None; GUI actions ARE enabled in this run", repair_prompt)
        self.assertIn("Use the JSON field", repair_prompt)
        self.assertIn("gui_start", repair_prompt)
        self.assertEqual(FakeGuiRuntime.instances[0].calls[0][0], "gui_start")


if __name__ == "__main__":
    unittest.main()
