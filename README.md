# P.A.V.L.U.S.H.A.

<p align="center">
  <img src="assets/pavlusha.png" alt="P.A.V.L.U.S.H.A. mascot holding a jar of jam" width="280">
</p>

Pavlusha is a project agent powered by a local or remote model API. This checkout
prepares version 0.9.0 and uses an event-driven kernel and trusted, pluggable
Python modules. See the [draft 0.9.0 notes](docs/release-0.9.0.md) and the
limitations below. The release is not yet published.

## Requirements and installation

Use Linux, Python 3.12, `bubblewrap`, and enabled user namespaces. A compatible model
server must be available for the agent to run. GUI tools also require Xvfb and X11;
ODT/ODS tools require LibreOffice and Python UNO; rendering also requires Poppler and fonts.
On systems where UNO is packaged separately, install the system Python UNO
bindings matching LibreOffice: the adapters invoke `/usr/bin/python3`, not the
project virtual environment. Writer image operations and Calc surface rendering
also need Pillow available to that system Python; surface rendering expects
DejaVu Sans fonts under `/usr/share/fonts/truetype/dejavu`. The draw.io module requires a local draw.io Desktop
executable for PNG export and a graphical environment. Neither application nor
model weights are bundled.

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
# For tests, also install:
pip install -r requirements-test.txt
python -m unittest discover -s tests
```

Check the version in the environment with `python agent.py --version` or
`python project_state_agent.py --version`. Both entry points use
`pavlusha_agent.__version__`. Installable Python package metadata is not yet provided;
run the commands from the project checkout.

## Running Pavlusha

Prepare separate, non-nested directories for the work files and the technical session.
Choose an assembly configuration with model, server and module settings:

```sh
python project_state_agent.py --config examples/liquid.cpu.config.json \
  --workdir /absolute/work --session-dir /absolute/session -- 'Describe the task'
```

Session journals can contain prompts, tool arguments, outputs and image data.
Protect the session directory using your operating system's permissions; Pavlusha
does not itself enforce a private mode on a supplied directory or every session file.
Do not publish session logs or place secrets in tool arguments.

Examples cover Liquid, Qwen, Ornith and Perplexity with a local child agent.
Modules are trusted Python code and are not sandboxed. `agent.py` runs a minimal
composition with its own CLI. Both entry points support `--help`.
For a managed llama-server, set `llama_server` in the assembly configuration
and adapt its paths and options to your system. The examples use placeholder
paths; the model and server are not included in the project.

The Perplexity API adapter is experimental and may contain bugs. API calls can
incur charges, including during long runs; usage, cache hits, and costs are not
currently displayed or limited automatically. Use it at your own risk and monitor
your provider balance. See [adapter notes](docs/perplexity-agent-adapter.md).

The modules provide Shell, Project State and Map, images, background processes,
GUI, Browser, Expert, ODT/ODS and draw.io tools, shell execution with model release, writing code
from reasoning, loop detection, and user input. Browser and Expert are inactive
by default and require configuration; see the [module capabilities](docs/loaded-capabilities.md)
and [module loader](docs/module-loader.md) documentation. Sandboxed shell network
access is controlled separately; external host processes such as Calc and draw.io
are not governed by that shell network setting.

The LibreOffice tools use revision-checked copies. Writer and Calc saves can change
complex document features. See [Writer](docs/writer-create.md),
[Calc](docs/libreoffice-adapter-prototype.md), and [draw.io](docs/drawio-module.md).
The Office module handles ODT/ODS; the former DOCX/XLSX tools are removed and
conversion to those formats is not supplied by the module.
The optional [programmatic module](docs/programmatic-module.md) runs bounded Python
with explicitly allowed tool calls. A local llama-server can be reused between
sequential sessions with [--keep-server](docs/persistent-server.md).

Experimental sequential delegation supports a Perplexity parent and a separately
running local child, as in `examples/arishka-pavlusha.config.json`. Local-to-local
delegation requires a managed llama-server with KV preservation and server control.
Child agents use separate workspaces and visible terminals. See
[orchestration](docs/orchestration.md) for setup, tested behavior and limits.

In an interactive terminal, Ctrl+Z requests a pause after the current turn, while
Ctrl+\ requests an urgent pause. Use `/multiline` and `/send` to enter several lines.
A paused session can resume with a user message. Streaming model messages to the user
and manual history editing have not been carried over.

## Interface and limitations

The [module interface](docs/event-interface.md) describes events, contributions, controls,
invocation context, observations, exclusive modes, and model release. Modules store
their state separately from the technical session; there is no shared crash-safe
transaction between them. An interrupted action with an unknown result blocks
automatic replay. To resume, use the same module configuration and retain both the
work directory and the session directory.

`shell_released` does not enforce the Project State review and context reset that
were mandatory in the previous implementation. Custom function packs and the history
bridge have not been carried over, and the old architecture is not maintained in
parallel. Compatibility with old sessions and configurations is not guaranteed.
See the [0.8.0 notes](docs/release-0.8.0.md).

The local [KV and MTP notes](docs/kv-storage.md) describe model-specific cache
settings and measurements. Earlier 0.7.0 results do not validate later changes.
