GUI_PROMPT = r"""
OPTIONAL PRIVATE GUI TOOLS
A private 800x600 X11 display is available for one GUI application when this toolkit is loaded.
These actions are ordinary work actions and are blocked by the same Project State review/checkpoint gates as shell/finish.

Start one GUI application inside the existing /work bubblewrap isolation:
{"action":"call_function","name":"gui_start","arguments":{"command":"python app.py","network":false,"delay":0.8}}
The command starts in /work. network=true requires the GUI toolkit allow_network startup grant.
Only one GUI session may be active at a time. It stays alive across reasoning, shell actions and reviews
until gui_close or controller termination. delay is only the initial settle wait. Keep the main GUI command in the foreground.
A server launched in the same gui_start sandbox belongs to that session and is cleaned up with it.
For web UIs use the explicit supported browser command: pavlusha-browser http://127.0.0.1:8000
It is provided by this toolkit only in gui_start (Epiphany, private D-Bus, software rendering).
Do not search the filesystem for browsers, use snap Firefox, or use /lib/chatgpt.
If pavlusha-browser reports a missing host dependency, report that error; do not invent a fallback.
Use network=true with the GUI toolkit network grant to reach a server started in a separate shell sandbox;
network=false has its own loopback and can reach only a server started in the same gui_start command.
gui_close closes the application and releases its private display and sandbox processes.

Observe or wait without another input gesture:
{"action":"call_function","name":"view_gui","arguments":{"delay":0}}
`delay` is 0..10 seconds. Use a positive delay when the UI needs time to render or finish an operation.

Act on the current 800x600 screenshot; each action waits `delay`, then the toolkit captures a fresh screenshot automatically:
{"action":"call_function","name":"click","arguments":{"x":400,"y":300,"delay":0.5}}
{"action":"call_function","name":"double_click","arguments":{"x":400,"y":300,"delay":0.5}}
{"action":"call_function","name":"right_click","arguments":{"x":400,"y":300,"delay":0.5}}
{"action":"call_function","name":"drag","arguments":{"x1":100,"y1":100,"x2":500,"y2":300,"delay":0.5}}
{"action":"call_function","name":"type_text","arguments":{"text":"printable text","delay":0.5}}
{"action":"call_function","name":"press_key","arguments":{"key":"enter","modifiers":[],"delay":0.5}}
{"action":"call_function","name":"hold_key","arguments":{"key":"right","duration":0.5,"delay":0.5}}
{"action":"call_function","name":"gui_close","arguments":{"delay":0.5}}
double_click sends two left clicks 50 ms apart in one action; delay is the wait after both clicks.
Coordinates are integer pixels. type_text is at most 64 printable characters and contains no control keys.
type_text enters text; press_key taps a physical key/combination; hold_key holds one physical key for a bounded duration.

Each successful capture attaches a fresh image to its chronological function result; images disappear when history is reset. Use view_gui to observe again. Image bytes are never Project State.
After click/double_click/right_click/drag, the toolkit draws a marker on the observation copy only:
- YOUR CLICK = the exact point where the previous left click was executed.
- RIGHT CLICK = the exact point where the previous right click was executed.
- START / END = hollow crosshairs marking where the previous drag began and ended; no path or arrow is drawn.
These labels/markers are toolkit annotations and are NOT part of the application UI. The clean screenshot is retained separately by the toolkit.
Only the immediately preceding mouse gesture is marked; view_gui returns a clean observation with no old marker.
Use the visible consequence plus the marker as feedback. If a click missed, correct the coordinates on the next action.
""".strip()
