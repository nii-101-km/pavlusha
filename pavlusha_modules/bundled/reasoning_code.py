"""Experimental reasoning code references, executed through the shell sandbox."""
import base64
import re
import shlex
import uuid
from collections import OrderedDict

from pavlusha_agent.core import AgentError

PROMPT = """
Experimental write_code_from_reasoning is enabled. Write the complete final file
ONCE in a normal Markdown fenced code block in your CURRENT reasoning, then return:
{"action":"write_code_from_reasoning","destination":"src/new.py","mode":"create"}.
Core selects the sole block automatically. If there are multiple fenced blocks,
include "block_index":2 to select the second block (numbering starts at 1, counting
ALL fenced blocks in current reasoning). Never guess: choose the block containing
the complete final file. No IDs are needed. An ordinary block looks like:
```python
print("hello")
```
The newline before the closing fence belongs to the file. Do not repeat the code
in the action. mode is create (refuse existing files) or overwrite (entire file).
History shows literal code blocks, never a generated reference/hash inventory.
For a write without an explicit saved reference, block_index selects ONLY blocks
in the CURRENT response, not historical blocks. Include the complete chosen file
as a fenced block in current reasoning, then issue the write action. Do not guess
which historical block belongs to a file. Use ordinary shell edits for partial
changes. If generation hits its token limit, Core retains fully closed blocks
but discards an unfinished trailing block; the truncated action is never executed.
Context resets and user history trim delete all internally saved refs.
"""


def extract_code_blocks(reasoning, *, allow_unclosed_tail=False):
    """Recognize line-based Markdown fences without changing payload whitespace."""
    blocks = []
    fence = None
    payload = []
    for line in reasoning.splitlines(keepends=True):
        stripped = line.rstrip("\r\n")
        if fence is None:
            match = re.fullmatch(r" {0,3}(`{3,}|~{3,})([^`\r\n]*)", stripped)
            if match:
                fence = match.group(1)
                payload = []
        elif re.fullmatch(r" {0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}[ \t]*", stripped):
            blocks.append("".join(payload))
            fence = None
        else:
            payload.append(line)
    if fence is not None and not allow_unclosed_tail:
        raise AgentError("unclosed Markdown fence in current reasoning; emit complete blocks before writing")
    return blocks


def extract_code_block(reasoning, block_index=None):
    blocks = extract_code_blocks(reasoning)
    if block_index is None:
        if len(blocks) != 1:
            raise AgentError(
                "current reasoning contains " + str(len(blocks)) +
                " complete fenced blocks; provide block_index (1-based) when multiple, "
                "or emit one complete block in this response")
        return blocks[0]
    if type(block_index) is not int or block_index < 1 or block_index > len(blocks):
        raise AgentError("block_index must select an existing block in current reasoning (1.." + str(len(blocks)) + ")")
    return blocks[block_index - 1]


def extract_single_block(reasoning):
    """Single-block compatibility helper; never silently select among several."""
    return extract_code_block(reasoning)


class ReasoningCodeStore:
    """Bounded, immutable references scoped to one runtime, never global history."""
    def __init__(self, max_bytes=1024 * 1024, max_blocks=64):
        self.blocks = OrderedDict()
        self.max_bytes = max_bytes
        self.max_blocks = max_blocks
        self.prefix = uuid.uuid4().hex[:12]
        self.serial = 0
        self.bytes = 0

    def capture(self, reasoning):
        # Retain only closed blocks; a trailing partial block never gets an ID.
        blocks = extract_code_blocks(reasoning, allow_unclosed_tail=True)
        added = []
        for index, code in enumerate(blocks, 1):
            size = len(code.encode("utf-8"))
            if size > self.max_bytes:
                continue
            self.serial += 1
            ref = self.prefix + ":" + str(self.serial)
            self.blocks[ref] = code
            self.bytes += size
            added.append((ref, code))
            while self.bytes > self.max_bytes or len(self.blocks) > self.max_blocks:
                _, old = self.blocks.popitem(last=False)
                self.bytes -= len(old.encode("utf-8"))
        # Show the actual source instead of opaque inventories. Use a longer fence
        # when the payload itself contains backticks, preserving payload bytes.
        entries = []
        for ref, code in added:
            if ref not in self.blocks:
                continue
            longest = max((len(m.group()) for m in re.finditer(r"`+", code)), default=0)
            fence = "`" * max(3, longest + 1)
            entries.append(fence + "\n" + code + fence)
        return ("CODE BLOCKS FROM THE PRECEDING RESPONSE:\n" +
                "\n".join(entries)) if entries else ""

    def clear(self):
        self.blocks.clear()
        self.bytes = 0
        # Keep the monotonic serial so forgotten IDs are never reused.

    def get(self, ref):
        if ref not in self.blocks:
            raise AgentError("unknown or expired block_ref in this controller run; no file written")
        return self.blocks[ref]


def validate_reference(action):
    required = {"action", "destination", "mode"}
    if not required.issubset(action) or set(action) - required - {"block_index", "block_ref"}:
        raise AgentError("code reference requires action, destination, mode and optional block_index OR block_ref")
    if "block_index" in action and (type(action["block_index"]) is not int or action["block_index"] < 1):
        raise AgentError("block_index must be a positive integer, starting at 1")
    if "block_ref" in action:
        if "block_index" in action or not isinstance(action["block_ref"], str) or not action["block_ref"]:
            raise AgentError("block_ref must be a nonempty string and cannot be combined with block_index")
    destination = action["destination"]
    if not isinstance(destination, str) or not destination or "\x00" in destination:
        raise AgentError("invalid destination")
    if action["mode"] not in ("create", "overwrite"):
        raise AgentError("mode must be create or overwrite")
    return "write_code_from_reasoning", dict(action)


def resolve_reference(data, reasoning, timeout, code_store=None):
    if "block_ref" in data:
        if code_store is None:
            raise AgentError("no saved code blocks available in this controller run")
        code = code_store.get(data["block_ref"])
    else:
        code = extract_code_block(reasoning, data.get("block_index"))
    payload = base64.b64encode(code.encode("utf-8")).decode("ascii")
    # This program runs INSIDE the existing Worker sandbox, never on the host.
    program = """import base64, os, sys, tempfile
from pathlib import Path
root = Path('/work').resolve()
p = Path(sys.argv[1])
p = (p if p.is_absolute() else root / p).resolve()
if not p.is_relative_to(root) or p == root:
    raise ValueError('destination must stay inside /work')
content = base64.b64decode(sys.argv[3])
if sys.argv[2] == 'create':
    with p.open('xb') as stream:
        stream.write(content)
else:
    fd, temporary = tempfile.mkstemp(dir=p.parent, prefix='.' + p.name + '.')
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            if p.exists():
                os.fchmod(stream.fileno(), p.stat().st_mode & 0o777)
        os.replace(temporary, p)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
print('Wrote', len(content), 'bytes to', p)
"""
    command = shlex.join(["python3", "-c", program, data["destination"], data["mode"], payload])
    return {"command": command, "network": False, "timeout": timeout}
