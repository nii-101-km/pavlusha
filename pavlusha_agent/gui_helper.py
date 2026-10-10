"""Trusted private-X11 helper derived from the NII runtime debugger GUI helper.

JSON is accepted only from the deterministic controller. Public action bounds live in gui.py.
"""
from __future__ import annotations

import io
import json
import signal
import sys
import time

# Public names refer to US-QWERTY physical positions, never current keysyms.
PHYSICAL_KEYS = {
    **{key: f'AD{i:02}' for i, key in enumerate('qwertyuiop', 1)},
    **{key: f'AC{i:02}' for i, key in enumerate('asdfghjkl', 1)},
    **{key: f'AB{i:02}' for i, key in enumerate('zxcvbnm', 1)},
    **{key: f'AE{i:02}' for i, key in enumerate('1234567890', 1)},
    **{f'f{i}': f'FK{i:02}' for i in range(1, 13)},
    'enter': 'RTRN', 'escape': 'ESC', 'tab': 'TAB', 'space': 'SPCE',
    'backspace': 'BKSP', 'delete': 'DELE', 'insert': 'INS',
    'home': 'HOME', 'end': 'END', 'page_up': 'PGUP', 'page_down': 'PGDN',
    'left': 'LEFT', 'right': 'RGHT', 'up': 'UP', 'down': 'DOWN',
}
MODIFIER_KEYS = {'ctrl': 'LCTL', 'shift': 'LFSH', 'alt': 'LALT', 'super': 'LWIN'}
MAX_HOLD_SECONDS = 2.0


def physical_keycodes(display_name, names):
    """Read XKB physical names via existing libX11; do not change any mapping.

    Prefix layouts from XkbDescRec/XkbNamesRec (X.Org libX11 XKB specification).
    The library allocates/frees the full records; we only read these prefixes.
    """
    import ctypes as c
    from ctypes.util import find_library

    class Names(c.Structure):
        _fields_ = [(field, c.c_ulong) for field in ('keycodes', 'geometry', 'symbols', 'types', 'compat')] + [
            ('vmods', c.c_ulong * 16), ('indicators', c.c_ulong * 32),
            ('groups', c.c_ulong * 4), ('keys', c.POINTER(c.c_char * 4))]

    class Desc(c.Structure):
        _fields_ = [('display', c.c_void_p), ('flags', c.c_ushort), ('device_spec', c.c_ushort),
                    ('min_key_code', c.c_ubyte), ('max_key_code', c.c_ubyte),
                    *[(field, c.c_void_p) for field in ('ctrls', 'server', 'map', 'indicators')],
                    ('names', c.POINTER(Names))]

    path = find_library('X11')
    if not path:
        raise RuntimeError('physical keyboard input requires libX11 with XKB')
    lib = c.CDLL(path)
    lib.XOpenDisplay.argtypes = [c.c_char_p]; lib.XOpenDisplay.restype = c.c_void_p
    lib.XCloseDisplay.argtypes = [c.c_void_p]
    lib.XkbGetMap.argtypes = [c.c_void_p, c.c_uint, c.c_uint]
    lib.XkbGetMap.restype = c.POINTER(Desc)
    lib.XkbGetNames.argtypes = [c.c_void_p, c.c_uint, c.POINTER(Desc)]
    lib.XkbGetNames.restype = c.c_int
    lib.XkbFreeKeyboard.argtypes = [c.POINTER(Desc), c.c_uint, c.c_int]
    connection = lib.XOpenDisplay(display_name.encode())
    if not connection:
        raise RuntimeError('cannot open private XKB display')
    keyboard = None
    try:
        # Fetch names from the current server map, without requesting a named keyboard.
        keyboard = lib.XkbGetMap(connection, 0, 0x100)  # UseCoreKbd
        if keyboard and lib.XkbGetNames(connection, 1 << 9, keyboard) != 0:
            raise RuntimeError('cannot read private XKB physical key names')
        if not keyboard or not keyboard.contents.names or not keyboard.contents.names.contents.keys:
            raise RuntimeError('private display does not provide XKB physical key names')
        desc = keyboard.contents
        mapping = {}
        for code in range(desc.min_key_code, desc.max_key_code + 1):
            name = bytes(desc.names.contents.keys[code]).rstrip(b'\0').decode('ascii')
            if name:
                mapping[name] = code
        try:
            return [mapping[name] for name in names]
        except KeyError as exc:
            raise ValueError(f'physical key unavailable on private display: {exc.args[0]}') from exc
    finally:
        if keyboard:
            lib.XkbFreeKeyboard(keyboard, 0, True)
        lib.XCloseDisplay(connection)


def keyboard(display, X, xtest, action, deadline):
    """Bounded physical event sequence; release all possibly pressed keys in reverse."""
    def interrupted(*_args):
        raise TimeoutError('Keyboard action interrupted or deadline expired')

    signals = (signal.SIGALRM, signal.SIGTERM, signal.SIGINT)
    previous = {sig: signal.getsignal(sig) for sig in signals}
    pressed = []
    try:
        for sig in signals:
            signal.signal(sig, interrupted)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Keyboard deadline expired')
        signal.setitimer(signal.ITIMER_REAL, remaining)
        names = [MODIFIER_KEYS[m] for m in action.get('modifiers', [])] + [PHYSICAL_KEYS[action['key']]]
        codes = physical_keycodes(display.get_display_name(), names)
        if action['action'] == 'release_keys':
            pressed.extend(codes)
        else:
            for code in codes:
                pressed.append(code)  # Include a request even if fake_input/sync raises.
                xtest.fake_input(display, X.KeyPress, code)
                display.sync()
            time.sleep(action['duration'] if action['action'] == 'hold_key' else 0.03)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        # Release itself must not be interrupted by the action's deadline/signals.
        for sig in signals:
            signal.signal(sig, signal.SIG_IGN)
        try:
            failures = []
            for code in reversed(pressed):
                try:
                    xtest.fake_input(display, X.KeyRelease, code)
                    display.sync()
                except Exception as exc:
                    failures.append(exc)
            if failures:
                raise RuntimeError('physical keyboard release failed') from failures[0]
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)


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


def double_click(display, X, xtest, action) -> None:
    """Two left clicks in one helper invocation, within the double-click interval."""
    click(display, X, xtest, action)
    time.sleep(0.05)
    click(display, X, xtest, action)


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
        elif kind == "double_click":
            double_click(d, X, xtest, action)
        elif kind == "right_click":
            right_click(d, X, xtest, action)
        elif kind == "drag":
            drag(d, X, xtest, action, float(sys.argv[1]))
        elif kind in {'press_key', 'hold_key', 'release_keys'}:
            keyboard(d, X, xtest, action, float(sys.argv[1]))
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
