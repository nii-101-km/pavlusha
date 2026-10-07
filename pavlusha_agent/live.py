"""Human-friendly append-only terminal rendering for live agent runs."""
from __future__ import annotations

import os
import json
import sys
import time
from typing import Any, TextIO

from . import __version__


class LiveConsoleRenderer:
    """Append-only renderer. It never owns or changes runtime state."""

    def __init__(self, *, stream: TextIO | None = None, color: bool | None = None) -> None:
        self.stream = stream or sys.stderr
        self.color = bool(self.stream.isatty() and color is not False
                          and "NO_COLOR" not in os.environ and os.getenv("TERM") != "dumb")
        # Optional at import time: non-live runs and older installations still work.
        try:
            from rich.console import Console
            from rich.theme import Theme
            self._console = Console(file=self.stream, force_terminal=self.color,
                                    color_system="standard" if self.color else None,
                                    force_interactive=False, markup=False, highlight=False,
                                    emoji=False, theme=Theme({
                                        "markdown.h1": "bold", "markdown.h2": "bold",
                                        "markdown.h3": "bold", "markdown.code": "cyan",
                                        "markdown.block_quote": "dim",
                                    }))
        except ImportError:
            self._console = None
        self._reasoning_open = False
        self._content_open = False
        self._started = time.monotonic()

    def _paint(self, style: str, text: str) -> str:
        code = {"heading": "1", "worker": "1", "shell": "1;36",
                "state": "1;36", "external": "1;35", "success": "1;32",
                "warning": "1;33", "error": "1;31", "metadata": "2",
                "user_chat": "1;96", "agent_chat": "1;95", "command": "1", "reasoning": "2"}[style]
        return f"\x1b[{code}m{text}\x1b[0m" if self.color else text

    def _markdown(self, text: str) -> None:
        if self._console is None:
            self._line(text)
            return
        from rich.markdown import Markdown
        self._console.print(Markdown(text, code_theme="ansi_dark"))
        self.stream.flush()

    def _status(self, status: str) -> str:
        style = {"DONE": "success", "ACTIVE": "heading", "PLANNED": "metadata",
                 "BLOCKED": "warning", "SUPERSEDED": "metadata"}.get(status, "metadata")
        return self._paint(style, status)

    def _literal(self, text: str, *, style: str | None = None) -> None:
        # Shell output is literal, including whitespace and Rich/Markdown-looking text.
        print(self._paint(style, text) if style else text, end="", file=self.stream, flush=True)
        if not text.endswith("\n"):
            self._line()

    def _line(self, text: str = "") -> None:
        print(text, file=self.stream, flush=True)

    def _stamp(self) -> str:
        return time.strftime("%H:%M:%S")

    def start(self, *, model: str, task: str, max_steps: int,
              context: int | None = None, expert_status: str = "off") -> None:
        title = "P.A.V.L.U.S.H.A.☝ 😐"
        description = (
            "Persistent Autonomous Verification & Local Utility\n"
            "Shell-Handling Agent"
        )
        context_text = str(context) if context is not None else "unknown"

        if self._console is not None:
            from rich.align import Align
            from rich.console import Group
            from rich.panel import Panel
            from rich.text import Text

            fields = Text()
            for label, value in (
                ("version", __version__),
                ("model", model),
                ("context", context_text),
                ("expert", expert_status),
            ):
                fields.append(f"{label:<9}", style="dim" if self.color else None)
                fields.append(value + ("\n" if label != "expert" else ""))

            content = Group(
                Text(""),
                Align.center(Text(title, style="bold" if self.color else None)),
                Text(""),
                Text(description),
                Text(""),
                fields,
                Text(""),
            )

            self._console.print(
                Panel(
                    content,
                    width=64,
                    padding=(0, 2),
                    border_style="dim" if self.color else "none",
                )
            )
            self.stream.flush()
        else:
            self._line(title)
            self._line(description)
            self._line(
                f"version {__version__} · model {model} · context {context_text} · expert {expert_status}"
            )

        self._line(self._paint("metadata", f"PAVLUSHA LIVE · max steps {max_steps}"))

        one_line = " ".join(task.split())
        if len(one_line) > 88:
            one_line = one_line[:85] + "..."
        self._line(self._paint("metadata", f"task  {one_line}"))

    def chat_message(self, speaker: str, text: str) -> None:
        self._close_streams()
        style = "user_chat" if speaker == "USER" else "agent_chat"
        self._line(self._paint(style, f"{speaker}:\n{text}"))

    def chat_status(self, text: str) -> None:
        self._close_streams()
        self._line(self._paint("warning", text))

    def step(self, step: int, max_steps: int, **legacy_telemetry: Any) -> None:
        self._close_streams()
        self._line()
        self._line(self._paint("heading", f"━━ STEP {step}/{max_steps} ━━"))

    def begin_worker(self) -> None:
        self._close_streams()
        self._line(f"{self._stamp()}  {self._paint('worker', 'WORKER')}  reasoning")
        self._reasoning_open = True

    def delta(self, kind: str, text: str) -> None:
        if not text:
            return
        if kind == "reasoning":
            if not self._reasoning_open:
                self.begin_worker()
            print(self._paint("reasoning", text), end="", file=self.stream, flush=True)
        elif kind == "content":
            if self._reasoning_open:
                self._line()
                self._reasoning_open = False
            if not self._content_open:
                self._line(f"{self._stamp()}  {self._paint('shell', 'ACTION')}")
                self._content_open = True
            print(text, end="", file=self.stream, flush=True)

    def _close_streams(self) -> None:
        if self._reasoning_open or self._content_open:
            self._line()
        self._reasoning_open = False
        self._content_open = False

    def reasoning_loop(self, signal: Any, *, mode: str, attempt: int) -> None:
        if not signal.confirmed and not signal.consecutive_matches and signal.word_count % 240:
            return
        self._close_streams()
        label = ("REASONING LOOP" if signal.confirmed else
                 "repetition suspected" if signal.consecutive_matches else "reasoning")
        self._line(f"  {label} · {signal.word_count} words · similarity {signal.similarity:.2f}"
                   f" · distance {signal.repetition_distance or 0} words · {mode} · attempt {attempt}")

    def prefix_changed(self, reason: str) -> None:
        """Render a runtime observation; this renderer owns no cache policy."""
        self._line(f"  PREFIX CHANGED · {reason}")

    def usage(self, *, prompt: int | None, context_budget: int, completion: int | None,
              reasoning: int | None) -> None:
        self._close_streams()
        p = "?" if prompt is None else str(prompt)
        c = "?" if completion is None else str(completion)
        r = "?" if reasoning is None else str(reasoning)
        pct = f" · {100.0 * prompt / context_budget:.1f}%" if prompt is not None else ""
        self._line(self._paint("metadata", f"  └─ prompt {p}/{context_budget}{pct} · completion {c} · reasoning {r}"))

    def gate(self, event: str, *, managed: int, budget: int, detail: str = "") -> None:
        self._close_streams()
        pct = 100.0 * managed / budget if budget else 0.0
        label = self._paint("warning", "CONTEXT")
        suffix = f" · {detail}" if detail else ""
        self._line(f"{self._stamp()}  {label}  gate {event.upper()} · replaceable {managed}/{budget} ({pct:.1f}%){suffix}")

    def action(self, kind: str, data: dict[str, Any]) -> None:
        self._close_streams()
        if kind == "shell":
            net = "net" if data.get("network") else "offline"
            self._line(f"{self._stamp()}  {self._paint('shell', 'SHELL')} [{net}]")
            self._line("  $ " + self._paint("command", str(data.get("command", ""))))
        elif kind == "drop_context":
            self._line(f"{self._stamp()}  {self._paint('warning', 'CONTEXT CLEANUP')}  requested")
        elif kind == "compact_context":
            self._line(f"{self._stamp()}  {self._paint('warning', 'CONTEXT COMPACT')}  requested")
        elif kind == "project_init":
            self._line(f"{self._stamp()}  {self._paint('state', 'PROJECT STATE')}  initial plan")
            for item in data.get("design", []):
                self._line("  " + self._paint("metadata", "DESIGN") + "  " + str(item.get("decision", "")))
            for index, item in enumerate(data.get("work", []), 1):
                status = str(item.get("status", "PLANNED"))
                self._line(f"  W?{index:02d} [{self._status(status)}]  {item.get('objective', '')}")
        elif kind == "project_update":
            self._line(f"{self._stamp()}  {self._paint('state', 'PROJECT STATE')}  update")
            for change in data.get("changes", []):
                op = str(change.get("op", ""))
                target = str(change.get("id", ""))
                status = str(change.get("status", ""))
                detail = " ".join(part for part in (op, target, self._status(status) if status else "") if part)
                self._line("  " + detail)
                evidence = change.get("evidence")
                if isinstance(evidence, list):
                    for item in evidence:
                        if isinstance(item, str) and item.strip():
                            self._line("    " + self._paint("metadata", "EVIDENCE") + "  " + item.strip())
        elif kind == "project_review_complete":
            self._line(f"{self._stamp()}  {self._paint('state', 'PROJECT STATE')}  review complete")
        elif kind in {"gui_start", "view_gui", "click", "right_click", "drag", "type_text", "gui_close", "press_key", "hold_key"}:
            self._line(f"{self._stamp()}  {self._paint('external', 'GUI')}  {kind}")
            coords = " ".join(f"{name}={data[name]}" for name in ("x", "y", "x1", "y1", "x2", "y2") if name in data)
            if coords:
                self._line("  " + coords)
            if kind in {'press_key', 'hold_key'}:
                self._line('  ' + '+'.join([*data.get('modifiers', []), data['key']]) +
                           (f" for {data['duration']:g}s" if kind == 'hold_key' else ''))
            if kind == "gui_start":
                self._line("  $ " + self._paint("command", str(data.get("command", ""))))
        elif kind == "call_function":
            self._line(f"{self._stamp()}  {self._paint('external', 'FUNCTION')}  {data.get('name')}")
        elif kind == "ask_expert":
            self._line(f"{self._stamp()}  {self._paint('external', 'EXPERT')}  requested")
        elif kind == "project_review_skip":
            self._line(f"{self._stamp()}  {self._paint('state', 'PROJECT STATE')}  {self._paint('warning', 'review skipped')}")
        elif kind == "finish":
            self._line(f"{self._stamp()}  {self._paint('success', 'FINISH')}  requested")

    def gui_result(self, result: dict[str, Any]) -> None:
        self._close_streams()
        state = str(result.get("state", ""))
        error = str(result.get("error", ""))
        if error:
            self._line(self._paint("error", f"  GUI ERROR · {error}"))
        else:
            suffix = f" · {state}" if state else ""
            self._line(self._paint("metadata", "  └─ GUI observation" + suffix))

    def expert_event(self, event: dict[str, Any]) -> None:
        if event.get("event") == "started":
            self._close_streams()
            self._line(f"{self._stamp()}  {self._paint('external', 'EXPERT')}  request started")
            self._line(self._paint("metadata", f"  model {event.get('model')} · call {event.get('calls')}/{event.get('max_calls')}"))

    def expert_result(self, result: dict[str, Any]) -> None:
        self._close_streams()
        error = result.get("error")
        self._line(f"{self._stamp()}  {self._paint('external', 'EXPERT')}  " +
                   self._paint("error" if error else "success", f"failed · {error}" if error else "returned"))
        if result.get("http_status") is not None:
            self._line(self._paint("metadata", f"  HTTP {result['http_status']}"))
        if result.get("duration_seconds") is not None:
            self._line(self._paint("metadata", f"  {result['duration_seconds']}s · prompt {result.get('prompt_tokens', '?')} · completion {result.get('completion_tokens', '?')}"))
        if isinstance(result.get("answer"), str):
            self._markdown(result["answer"])

    def function_result(self, op_id: str, result: dict[str, Any]) -> None:
        self._close_streams()
        self._literal(json.dumps(result, ensure_ascii=False))
        error = result.get("error")
        status = f"failed · {error}" if error else "returned"
        self._line(f"  {op_id} · " + self._paint("error" if error else "success", status))

    def operation(self, op_id: str, result: dict[str, Any]) -> None:
        self._close_streams()
        stdout = str(result.get("stdout", ""))
        stderr = str(result.get("stderr", ""))
        if stdout:
            self._literal(stdout)
        if stderr:
            self._line(self._paint("warning", "  stderr:"))
            self._literal(stderr)
        if result.get("output_withheld"):
            total = result.get("output_chars")
            limit = result.get("output_limit_chars")
            self._line(self._paint("warning", f"  OUTPUT WITHHELD · {total} chars > {limit} char door"))
            self._line("  Use Project Map and a narrower query; full output was not admitted to Worker context.")
        exit_code = result.get("exit_code")
        timed_out = bool(result.get("timed_out", False))
        duration = result.get("duration_seconds")
        mark = self._paint("success", "✓") if exit_code == 0 and not timed_out else self._paint("error", "✗")
        extra = f" · {duration}s" if duration is not None else ""
        self._line(f"  {mark} " + self._paint("metadata", f"{op_id} · exit {exit_code}{extra}"))

    def context_drop(self, disposition_id: str, *, handles: list[str], tokens: int,
                     intent: str, managed_after: int, budget: int) -> None:
        self._close_streams()
        self._line(f"{self._stamp()}  {self._paint('warning', 'CONTEXT')}  {disposition_id}")
        self._line(f"  removed {', '.join(handles)} · freed ~{tokens} tok")
        self._line(f"  reason  {intent}")
        self._line(f"  replaceable {managed_after}/{budget}")

    def context_replacement(
        self,
        disposition_id: str,
        *,
        mode: str,
        items: list[dict[str, Any]],
        tokens_before: int,
        tokens_after: int,
        managed_after: int,
        budget: int,
    ) -> None:
        """Render in-place context replacement; owns no context/cache semantics."""
        self._close_streams()
        label = "TOMBSTONE" if mode == "tombstone" else "COMPACTED"
        self._line(f"{self._stamp()}  {self._paint('warning', 'CONTEXT')}  {disposition_id}")
        for item in items:
            handle = str(item.get("handle", ""))
            before = int(item.get("original_approx_tokens", 0) or 0)
            after = int(item.get("replacement_approx_tokens", 0) or 0)
            note = str(item.get("note", ""))
            self._line(f"  {handle}  RAW ~{before} tok → {label} ~{after} tok")
            self._line(f"    {note}")
        freed = tokens_before - tokens_after
        self._line(f"  net freed ~{freed} tok · replaceable {managed_after}/{budget}")

    def invalid(self, message: str) -> None:
        self._close_streams()
        self._line(f"{self._stamp()}  {self._paint('error', 'INVALID ACTION')}  {message}")

    def complete(self, summary: str) -> None:
        self._close_streams()
        elapsed = time.monotonic() - self._started
        self._line()
        self._line("╭─ " + self._paint("success", "COMPLETE") + " " + "─" * 50)
        self._markdown(summary)
        self._line(self._paint("metadata", f"│ elapsed {elapsed:.1f}s"))
        self._line("╰" + "─" * 63)
