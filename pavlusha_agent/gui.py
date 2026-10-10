"""Deterministic private-display GUI tools for the Worker.

The low-level X11 mechanics are adapted from nii-runtime-debugger's live_gui.py/gui_helper.py.
No model/controller from NII is imported: Pavlusha remains the only Worker.  GUI screenshots are
fresh observations, not semantic memory or recovery authority.
"""
from __future__ import annotations

import base64
import io
import json
import math
import os
import secrets
import shutil
import signal
import socket
import struct
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .core import AgentError, _trim
from .sandbox import build_bwrap_command
from .gui_helper import PHYSICAL_KEYS, MODIFIER_KEYS, MAX_HOLD_SECONDS

WIDTH, HEIGHT = 800, 600
MAX_DELAY = 10.0
DEFAULT_DELAY = 0.5
MAX_TEXT = 64
MAX_IMAGE_BYTES = 12 * 1024 * 1024
GUI_HELPER_TIMEOUT = 3.0
GUI_ACTIONS = {
    "gui_start", "view_gui", "click", "double_click", "right_click", "drag", "type_text", "gui_close",
    "press_key", "hold_key",
}


class GuiError(AgentError):
    pass


def ensure_gui_dependencies() -> None:
    """Fail before model work when --gui advertises tools the host cannot provide."""
    if shutil.which("Xvfb") is None:
        raise GuiError("--gui requires Xvfb on the controller host")
    try:
        import Xlib  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError as exc:
        raise GuiError("--gui requires python-xlib and Pillow in the controller environment") from exc


def _delay(value: Any, *, default: float = DEFAULT_DELAY) -> float:
    if value is None:
        value = default
    if type(value) not in (int, float) or not math.isfinite(value):
        raise GuiError("GUI delay must be a finite number")
    value = float(value)
    if not 0.0 <= value <= MAX_DELAY:
        raise GuiError(f"GUI delay must be between 0 and {MAX_DELAY:g} seconds")
    return value


def _coord(value: Any, *, name: str, bound: int) -> int:
    if type(value) is not int or not 0 <= value < bound:
        raise GuiError(f"GUI {name} must be an integer in 0..{bound - 1}")
    return value


def validate_gui_action(action: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Validate the public Worker GUI contract and normalize defaults."""
    if not isinstance(action, dict):
        raise GuiError("GUI action must be a JSON object")
    kind = action.get("action")
    if kind not in GUI_ACTIONS:
        raise GuiError("not a GUI action")

    if kind == "gui_start":
        allowed = {"action", "command", "network", "delay"}
        if set(action) - allowed:
            raise GuiError("gui_start has unsupported fields")
        command = action.get("command")
        if not isinstance(command, str) or not command.strip():
            raise GuiError("gui_start requires a non-empty command")
        network = action.get("network", False)
        if not isinstance(network, bool):
            raise GuiError("gui_start.network must be true or false")
        return kind, {
            "command": command.strip(), "network": network,
            "delay": _delay(action.get("delay"), default=DEFAULT_DELAY),
        }

    if kind == "view_gui":
        if set(action) - {"action", "delay"}:
            raise GuiError("view_gui has unsupported fields")
        return kind, {"delay": _delay(action.get("delay"), default=0.0)}

    if kind in {"click", "double_click", "right_click"}:
        if set(action) - {"action", "x", "y", "delay"}:
            raise GuiError(f"{kind} has unsupported fields")
        if "x" not in action or "y" not in action:
            raise GuiError(f"{kind} requires x and y")
        return kind, {
            "x": _coord(action["x"], name="x", bound=WIDTH),
            "y": _coord(action["y"], name="y", bound=HEIGHT),
            "delay": _delay(action.get("delay")),
        }

    if kind == "drag":
        if set(action) - {"action", "x1", "y1", "x2", "y2", "delay"}:
            raise GuiError("drag has unsupported fields")
        for name in ("x1", "y1", "x2", "y2"):
            if name not in action:
                raise GuiError("drag requires x1, y1, x2 and y2")
        return kind, {
            "x1": _coord(action["x1"], name="x1", bound=WIDTH),
            "y1": _coord(action["y1"], name="y1", bound=HEIGHT),
            "x2": _coord(action["x2"], name="x2", bound=WIDTH),
            "y2": _coord(action["y2"], name="y2", bound=HEIGHT),
            "delay": _delay(action.get("delay")),
        }

    if kind in {'press_key', 'hold_key'}:
        allowed = {'action', 'key', 'delay', 'modifiers' if kind == 'press_key' else 'duration'}
        if set(action) - allowed:
            raise GuiError(f'{kind} has unsupported fields')
        key = action.get('key')
        if not isinstance(key, str) or key not in PHYSICAL_KEYS:
            raise GuiError('unsupported physical key name')
        data = {'key': key, 'delay': _delay(action.get('delay'))}
        if kind == 'press_key':
            modifiers = action.get('modifiers', [])
            if (not isinstance(modifiers, list) or len(modifiers) > len(MODIFIER_KEYS)
                    or any(not isinstance(m, str) or m not in MODIFIER_KEYS for m in modifiers)
                    or len(set(modifiers)) != len(modifiers)):
                raise GuiError('modifiers must be distinct supported modifier names')
            data['modifiers'] = [m for m in MODIFIER_KEYS if m in modifiers]
        else:
            duration = action.get('duration')
            if (type(duration) not in (int, float) or not math.isfinite(duration)
                    or not 0 < duration <= MAX_HOLD_SECONDS):
                raise GuiError(f'hold_key.duration must be finite and > 0, <= {MAX_HOLD_SECONDS:g} seconds')
            data['duration'] = float(duration)
        return kind, data

    if kind == "type_text":
        if set(action) - {"action", "text", "delay"}:
            raise GuiError("type_text has unsupported fields")
        text = action.get("text")
        if (
            not isinstance(text, str)
            or len(text) > MAX_TEXT
            or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in text)
        ):
            raise GuiError(f"type_text.text must be at most {MAX_TEXT} printable characters with no control keys")
        return kind, {"text": text, "delay": _delay(action.get("delay"))}

    if kind == "gui_close":
        if set(action) - {"action", "delay"}:
            raise GuiError("gui_close has unsupported fields")
        return kind, {"delay": _delay(action.get("delay"))}

    raise GuiError("unsupported GUI action")


class PrivateDisplay:
    """One controller-owned Xvfb display. Never points at the host desktop."""

    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.environment = dict(os.environ)
        self.server: subprocess.Popen[bytes] | None = None
        self.auth = self.directory / ".gui-xauthority"
        self.number: int | None = None
        self.socket_path: Path | None = None

    def __enter__(self) -> "PrivateDisplay":
        # A few bounded retries avoid turning an ordinary display-number collision into failure.
        last_error: Exception | None = None
        for _ in range(8):
            try:
                number = 1000 + secrets.randbelow(8000)
                socket_path = Path(f"/tmp/.X11-unix/X{number}")
                if socket_path.exists():
                    continue
                self.auth.unlink(missing_ok=True)
                cookie = secrets.token_bytes(16)
                parts = (socket.gethostname().encode(), str(number).encode(), b"MIT-MAGIC-COOKIE-1", cookie)
                record = struct.pack(">H", 256) + b"".join(struct.pack(">H", len(p)) + p for p in parts)
                fd = os.open(self.auth, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(record)
                environment = dict(os.environ)
                environment.update(
                    DISPLAY=f":{number}", XAUTHORITY=str(self.auth), GDK_BACKEND="x11",
                    QT_QPA_PLATFORM="xcb", SDL_VIDEODRIVER="x11",
                    DBUS_SESSION_BUS_ADDRESS="unix:path=/nonexistent/pavlusha-gui-bus",
                )
                environment.pop("WAYLAND_DISPLAY", None)
                server = subprocess.Popen(
                    [os.getenv("PAVLUSHA_GUI_XVFB", "Xvfb"), f":{number}", "-screen", "0",
                     f"{WIDTH}x{HEIGHT}x24", "-nolisten", "tcp", "-auth", str(self.auth)],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                self.server = server
                deadline = time.monotonic() + 3.0
                while not socket_path.exists():
                    if server.poll() is not None or time.monotonic() >= deadline:
                        raise GuiError("private Xvfb display did not start")
                    time.sleep(0.02)
                self.environment = environment
                self.server = server
                self.number = number
                self.socket_path = socket_path
                return self
            except (OSError, GuiError) as exc:
                last_error = exc
                if self.server is not None and self.server.poll() is None:
                    self.server.terminate()
                    try:
                        self.server.wait(timeout=0.5)
                    except subprocess.TimeoutExpired:
                        self.server.kill()
                        self.server.wait()
                self.server = None
                self.auth.unlink(missing_ok=True)
        raise GuiError("GUI setup failed; private Xvfb display is required") from last_error

    def __exit__(self, *_args) -> None:
        if self.server is not None and self.server.poll() is None:
            self.server.terminate()
            try:
                self.server.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                self.server.kill()
                self.server.wait()
        self.server = None
        self.auth.unlink(missing_ok=True)

    def action(self, action: dict[str, Any], timeout: float) -> bytes:
        command = [sys.executable, "-B", str(Path(__file__).with_name("gui_helper.py"))]
        if action["action"] in {"drag", "press_key", "hold_key", "release_keys"}:
            # Trusted helper deadline, not model supplied. Reserve a little release time.
            command.append(str(time.monotonic() + min(timeout, 3.0) - 0.1))
        try:
            result = subprocess.run(
                command,
                input=json.dumps(action, ensure_ascii=False).encode("utf-8"),
                capture_output=True,
                env=self.environment,
                # Terminal Ctrl+Z belongs to Core; never suspend an active helper.
                start_new_session=True,
                timeout=min(timeout, 3.0),
                check=False,
            )
        except (KeyboardInterrupt, SystemExit):
            if action['action'] in {'press_key', 'hold_key'}:
                self._release_keyboard(action)
            raise
        except (OSError, subprocess.TimeoutExpired) as exc:
            if action['action'] in {'press_key', 'hold_key'}:
                self._release_keyboard(action)
            raise GuiError("private GUI helper unavailable or timed out") from exc
        if result.returncode:
            if action['action'] in {'press_key', 'hold_key'}:
                self._release_keyboard(action)
            detail = result.stderr.decode("utf-8", "replace") if result.stderr else ""
            raise GuiError("private GUI helper failed: " + _trim(detail, 500))
        return result.stdout

    def _release_keyboard(self, action):
        # A killed/failed helper may not have reached its finally. Use a new bounded
        # connection, with no press. If X cannot confirm release, retire its display.
        cleanup = {'action': 'release_keys', 'key': action['key'],
                   'modifiers': action.get('modifiers', [])}
        try:
            self.action(cleanup, 1.0)
        except GuiError as exc:
            self.__exit__(None, None, None)
            raise GuiError('keyboard release could not be confirmed; private display closed') from exc


def _font():
    from PIL import ImageFont

    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    ):
        try:
            return ImageFont.truetype(path, 14)
        except OSError:
            pass
    return ImageFont.load_default()


def _label(draw, xy: tuple[int, int], text: str, image_size: tuple[int, int]) -> None:
    font = _font()
    x, y = xy
    try:
        box = draw.textbbox((0, 0), text, font=font, stroke_width=1)
        width, height = box[2] - box[0], box[3] - box[1]
    except AttributeError:
        width, height = draw.textsize(text, font=font)
    pad = 4
    left = max(0, min(x, image_size[0] - width - 2 * pad))
    top = max(0, min(y, image_size[1] - height - 2 * pad))
    draw.rounded_rectangle(
        (left, top, left + width + 2 * pad, top + height + 2 * pad),
        radius=3, fill=(0, 0, 0, 210), outline=(255, 255, 255, 235), width=1,
    )
    draw.text((left + pad, top + pad), text, font=font, fill=(255, 255, 255, 255), stroke_width=1,
              stroke_fill=(0, 0, 0, 255))


def annotate_png(clean_png: bytes, gesture: dict[str, Any] | None) -> bytes:
    """Return an observation copy with only the immediately preceding mouse gesture marked."""
    if gesture is None:
        return clean_png
    from PIL import Image, ImageDraw

    with Image.open(io.BytesIO(clean_png)) as source:
        image = source.convert("RGBA")
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    accent = (255, 40, 180, 255)
    kind = gesture.get("action")

    if kind in {"click", "double_click", "right_click"}:
        x, y = int(gesture["x"]), int(gesture["y"])
        radius = 10
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), outline=accent, width=4)
        draw.line((x - 15, y, x + 15, y), fill=accent, width=2)
        draw.line((x, y - 15, x, y + 15), fill=accent, width=2)
        _label(draw, (x + 14, y + 12), {"click": "YOUR CLICK", "double_click": "DOUBLE CLICK", "right_click": "RIGHT CLICK"}[kind], image.size)
    elif kind == "drag":
        x1, y1, x2, y2 = (int(gesture[name]) for name in ("x1", "y1", "x2", "y2"))
        draw.line((x1, y1, x2, y2), fill=accent, width=4)
        draw.ellipse((x1 - 6, y1 - 6, x1 + 6, y1 + 6), fill=accent)
        # Small arrow head at the actual release point.
        dx, dy = x2 - x1, y2 - y1
        length = max(1.0, math.hypot(dx, dy))
        ux, uy = dx / length, dy / length
        px, py = -uy, ux
        back_x, back_y = x2 - ux * 15, y2 - uy * 15
        points = [
            (x2, y2),
            (round(back_x + px * 7), round(back_y + py * 7)),
            (round(back_x - px * 7), round(back_y - py * 7)),
        ]
        draw.polygon(points, fill=accent)
        draw.ellipse((x2 - 5, y2 - 5, x2 + 5, y2 + 5), outline=(255, 255, 255, 255), width=2)
        _label(draw, (round((x1 + x2) / 2) + 10, round((y1 + y2) / 2) + 10), "DRAG", image.size)

    image = Image.alpha_composite(image, overlay).convert("RGB")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


@dataclass(frozen=True)
class GuiObservation:
    clean_png: bytes
    observation_png: bytes
    gesture: dict[str, Any] | None
    captured_at: float

    def message(self) -> dict[str, Any]:
        kind = self.gesture.get("action") if self.gesture else None
        label = {
            "click": "YOUR CLICK",
            "double_click": "DOUBLE CLICK",
            "right_click": "RIGHT CLICK",
            "drag": "DRAG",
        }.get(kind)
        annotation = (
            f" The visible {label} marker is a Core annotation of your immediately preceding mouse action, "
            "not part of the application UI."
            if label else
            " No previous mouse-action marker is overlaid on this frame."
        )
        encoded = base64.b64encode(self.observation_png).decode("ascii")
        return {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"CURRENT GUI OBSERVATION ({WIDTH}x{HEIGHT}). This is a fresh post-action screenshot."
                        + annotation
                    ),
                },
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + encoded}},
            ],
        }


class GuiRuntime:
    """One optional long-lived GUI application inside the existing bubblewrap policy."""

    def __init__(self, workdir: Path, state_dir: Path, *, network_allowed: bool):
        ensure_gui_dependencies()
        self.workdir = Path(workdir)
        self.directory = Path(state_dir) / "gui-runtime"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.network_allowed = network_allowed
        self.display: PrivateDisplay | None = None
        self.process: subprocess.Popen[bytes] | None = None
        self.stdout_handle = None
        self.stderr_handle = None
        self.stdout_path = self.directory / "current.stdout.log"
        self.stderr_path = self.directory / "current.stderr.log"
        self.latest: GuiObservation | None = None

    def __enter__(self) -> "GuiRuntime":
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    def _tails(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for key, path in (("stdout_tail", self.stdout_path), ("stderr_tail", self.stderr_path)):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if text:
                result[key] = _trim(text, 4000)
        return result

    def _process_state(self) -> dict[str, Any]:
        if self.process is None:
            return {"state": "not_started"}
        code = self.process.poll()
        if code is not None:
            return {"state": "exited", "exit_code": code, **self._tails()}
        return {"state": "alive"}

    def _terminate_process(self) -> None:
        process = self.process
        if process is None or process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=0.7)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()

    def close(self) -> None:
        self.latest = None
        self._terminate_process()
        self.process = None
        for handle_name in ("stdout_handle", "stderr_handle"):
            handle = getattr(self, handle_name)
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass
                setattr(self, handle_name, None)
        if self.display is not None:
            self.display.__exit__()
            self.display = None

    def _reset_dead_session(self) -> None:
        self.latest = None
        if self.process is not None and self.process.poll() is None:
            return
        self.process = None
        for handle_name in ("stdout_handle", "stderr_handle"):
            handle = getattr(self, handle_name)
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass
                setattr(self, handle_name, None)
        if self.display is not None:
            self.display.__exit__()
            self.display = None

    def _bwrap_argv(self, command: str, *, network: bool) -> list[str]:
        if self.display is None or self.display.socket_path is None or self.display.number is None:
            raise GuiError("private display is not active")
        argv = build_bwrap_command(self.workdir, command, network=network)
        try:
            command_index = argv.index("/bin/bash")
        except ValueError as exc:
            raise GuiError("internal bubblewrap command layout changed") from exc
        auth_inside = "/tmp/.pavlusha-xauthority"
        extra = [
            "--dir", "/tmp/.pavlusha-gui-bin",
            "--ro-bind", str(Path(__file__).with_name("gui_browser.sh")),
            "/tmp/.pavlusha-gui-bin/pavlusha-browser",
            "--setenv", "PATH", "/tmp/.pavlusha-gui-bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "--setenv", "PAVLUSHA_PRIVATE_GUI", "1",
            "--dir", "/tmp/.X11-unix",
            "--ro-bind", str(self.display.socket_path), str(self.display.socket_path),
            "--ro-bind", str(self.display.auth), auth_inside,
            "--setenv", "DISPLAY", f":{self.display.number}",
            "--setenv", "XAUTHORITY", auth_inside,
            "--setenv", "GDK_BACKEND", "x11",
            "--setenv", "QT_QPA_PLATFORM", "xcb",
            "--setenv", "SDL_VIDEODRIVER", "x11",
            "--setenv", "DBUS_SESSION_BUS_ADDRESS", "unix:path=/nonexistent/pavlusha-gui-bus",
        ]
        argv[command_index:command_index] = extra
        return argv

    def _wait_settle(self, delay: float) -> dict[str, Any]:
        state = self._process_state()
        if state["state"] != "alive":
            return state
        if delay <= 0:
            return state
        try:
            self.process.wait(timeout=delay)  # type: ignore[union-attr]
        except subprocess.TimeoutExpired:
            pass
        return self._process_state()

    def _capture(self, gesture: dict[str, Any] | None) -> GuiObservation:
        if self.display is None:
            raise GuiError("GUI session is not active")
        data = self.display.action({"action": "screenshot"}, GUI_HELPER_TIMEOUT)
        if not data or len(data) > MAX_IMAGE_BYTES or not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise GuiError("invalid or oversized GUI screenshot")
        observation = annotate_png(data, gesture)
        item = GuiObservation(data, observation, gesture, time.time())
        self.latest = item
        # Diagnostic copies are controller-owned and never recovery authority.
        try:
            (self.directory / "latest-clean.png").write_bytes(data)
            (self.directory / "latest-observation.png").write_bytes(observation)
        except OSError:
            pass
        return item

    def start(self, data: dict[str, Any]) -> tuple[dict[str, Any], GuiObservation | None]:
        self._reset_dead_session()
        if self.process is not None and self.process.poll() is None:
            return {"error": "gui_session_already_active", "state": "alive"}, None
        if data["network"] and not self.network_allowed:
            return {
                "error": "network_not_granted",
                "hint": "Run the controller with --network before requesting networked gui_start.",
            }, None
        self.display = PrivateDisplay(self.directory)
        try:
            self.display.__enter__()
            argv = self._bwrap_argv(data["command"], network=data["network"])
            self.stdout_handle = self.stdout_path.open("wb")
            self.stderr_handle = self.stderr_path.open("wb")
            self.process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=self.stdout_handle,
                stderr=self.stderr_handle,
                start_new_session=True,
            )
            state = self._wait_settle(data["delay"])
            if state["state"] != "alive":
                self.latest = None
                self.close()
                return {"action": "gui_start", "command": data["command"], **state}, None
            observation = self._capture(None)
            return {
                "action": "gui_start", "state": "alive", "command": data["command"],
                "network": data["network"], "delay": data["delay"],
                "display": {"width": WIDTH, "height": HEIGHT},
                "observation": "fresh screenshot attached",
            }, observation
        except (GuiError, OSError, subprocess.SubprocessError) as exc:
            self.close()
            return {
                "action": "gui_start",
                "error": str(exc) or type(exc).__name__,
                "state": "not_started",
            }, None

    def execute(self, kind: str, data: dict[str, Any]) -> tuple[dict[str, Any], GuiObservation | None]:
        if kind == "gui_start":
            return self.start(data)
        state = self._process_state()
        if state["state"] != "alive":
            self.latest = None
            if kind == "gui_close":
                self.close()
                return {"action": kind, **state, "state": "closed"}, None
            return {"action": kind, **state}, None
        if self.display is None:
            self.latest = None
            return {"action": kind, "error": "gui_session_not_started"}, None

        try:
            if kind == "view_gui":
                state = self._wait_settle(data["delay"])
                if state["state"] != "alive":
                    self.latest = None
                    return {"action": kind, **state}, None
                observation = self._capture(None)
                return {
                    "action": kind, "state": "alive", "delay": data["delay"],
                    "observation": "fresh screenshot attached",
                }, observation

            helper: dict[str, Any]
            gesture: dict[str, Any] | None = None
            if kind in {"click", "double_click", "right_click"}:
                helper = {"action": kind, "x": data["x"], "y": data["y"]}
                gesture = dict(helper)
            elif kind == "drag":
                helper = {"action": kind, **{name: data[name] for name in ("x1", "y1", "x2", "y2")}}
                gesture = dict(helper)
            elif kind == "type_text":
                helper = {"action": kind, "text": data["text"]}
            elif kind in {'press_key', 'hold_key'}:
                helper = {'action': kind, **{name: data[name] for name in ('key', 'modifiers', 'duration') if name in data}}
            elif kind == "gui_close":
                helper = {"action": "close"}
            else:
                raise GuiError("unsupported GUI action")

            self.display.action(helper, GUI_HELPER_TIMEOUT)
            state = self._wait_settle(data["delay"])
            if state["state"] != "alive":
                self.latest = None
                if kind == "gui_close":
                    self.close()
                    return {"action": kind, **state, "state": "closed"}, None
                return {"action": kind, "delay": data["delay"], **state}, None
            observation = self._capture(gesture)
            result: dict[str, Any] = {
                "action": kind, "state": "alive", "delay": data["delay"],
                "observation": "fresh screenshot attached",
            }
            for name in ("x", "y", "x1", "y1", "x2", "y2"):
                if name in data:
                    result[name] = data[name]
            if kind == "type_text":
                result["text_chars"] = len(data["text"])
            if kind in {'press_key', 'hold_key'}:
                result.update({name: data[name] for name in ('key', 'modifiers', 'duration') if name in data})
            if kind == "gui_close":
                self.close()
                result["state"] = "closed"
            return result, observation
        except GuiError as exc:
            self.latest = None
            state = self._process_state()
            if kind == "gui_close":
                self.close()
                state["state"] = "closed"
            return {"action": kind, "error": str(exc), **state}, None
