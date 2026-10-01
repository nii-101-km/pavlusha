"""Trusted private-X11 helper derived from the NII runtime debugger GUI helper.

JSON is accepted only from the deterministic controller. Public action bounds live in gui.py.
"""
from __future__ import annotations

import io
import json
import signal
import sys
import time


def drag(display, X, xtest, action, deadline: float) -> None:
    """One bounded left-button drag; always attempt release after a possible press."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Drag deadline expired")

    def expired(*_args):
        raise TimeoutError("Drag deadline expired")

    previous = signal.signal(signal.SIGALRM, expired)
    pressed = False
    try:
        signal.setitimer(signal.ITIMER_REAL, remaining)
        xtest.fake_input(display, X.MotionNotify, x=action["x1"], y=action["y1"])
        display.sync()
        pressed = True  # The press request may reach X even if a later sync fails.
        xtest.fake_input(display, X.ButtonPress, 1)
        display.sync()
        time.sleep(0.02)
        for step in range(1, 11):
            x = round(action["x1"] + (action["x2"] - action["x1"]) * step / 10)
            y = round(action["y1"] + (action["y2"] - action["y1"]) * step / 10)
            xtest.fake_input(display, X.MotionNotify, x=x, y=y)
            display.sync()
            time.sleep(0.02)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        try:
            if pressed:
                xtest.fake_input(display, X.ButtonRelease, 1)
                display.sync()
        finally:
            signal.signal(signal.SIGALRM, previous)


def click(display, X, xtest, action) -> None:
    """One ordinary left click; release even if press/sync raises."""
    xtest.fake_input(display, X.MotionNotify, x=action["x"], y=action["y"])
    display.sync()
    pressed = False
    try:
        pressed = True  # The press request may reach X even if a later sync fails.
        xtest.fake_input(display, X.ButtonPress, 1)
        display.sync()
    finally:
        if pressed:
            xtest.fake_input(display, X.ButtonRelease, 1)
            display.sync()


def right_click(display, X, xtest, action) -> None:
    """One ordinary right click; release even if press/sync raises."""
    xtest.fake_input(display, X.MotionNotify, x=action["x"], y=action["y"])
    display.sync()
    try:
        xtest.fake_input(display, X.ButtonPress, 3)
        display.sync()
    finally:
        xtest.fake_input(display, X.ButtonRelease, 3)
        display.sync()


def main() -> None:
    from Xlib import X, display, protocol
    from Xlib.ext import xtest
    from PIL import ImageGrab

    action = json.loads(sys.stdin.read(8192))
    d = display.Display()
    root = d.screen().root
    kind = action["action"]
    try:
        if kind == "screenshot":
            image = ImageGrab.grab(xdisplay=d.get_display_name())
            output = io.BytesIO()
            image.save(output, format="PNG")
            sys.stdout.buffer.write(output.getvalue())
        elif kind == "click":
            click(d, X, xtest, action)
        elif kind == "right_click":
            right_click(d, X, xtest, action)
        elif kind == "drag":
            drag(d, X, xtest, action, float(sys.argv[1]))
        elif kind == "type_text":
            # One temporary keycode, only on the isolated X server. No clipboard/hotkeys.
            keycode = d.display.info.max_keycode
            original = d.get_keyboard_mapping(keycode, 1)
            try:
                for character in action["text"]:
                    value = ord(character)
                    keysym = value if value < 256 else 0x01000000 | value
                    # XKB expands a single uppercase symbol into lower/upper levels.
                    # XTest sends no Shift, so give both levels the literal character.
                    d.change_keyboard_mapping(keycode, [(keysym, keysym)])
                    d.sync()
                    time.sleep(0.02)
                    xtest.fake_input(d, X.KeyPress, keycode)
                    xtest.fake_input(d, X.KeyRelease, keycode)
                    d.sync()
                    time.sleep(0.02)
            finally:
                d.change_keyboard_mapping(keycode, original)
                d.sync()
        elif kind == "close":
            # The private display belongs to one GUI session. Ask visible clients to close.
            delete = d.intern_atom("WM_DELETE_WINDOW")
            protocols = d.intern_atom("WM_PROTOCOLS")
            for window in root.query_tree().children[:64]:
                if delete in (window.get_wm_protocols() or []):
                    window.send_event(
                        protocol.event.ClientMessage(
                            window=window,
                            client_type=protocols,
                            data=(32, [delete, X.CurrentTime, 0, 0, 0]),
                        )
                    )
            d.sync()
        else:
            raise ValueError("Unsupported helper action")
    finally:
        d.close()


if __name__ == "__main__":
    main()
