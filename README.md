# P.A.V.L.U.S.H.A.

Pavlusha is a local project agent powered by a model with a Chat Completions-compatible
API. Version 0.7.0 uses an event-driven kernel and trusted, pluggable Python modules.
See the [release notes](docs/release-0.7.0.md) and the limitations below.

## Requirements and installation

Use Linux, Python 3.12, `bubblewrap`, and enabled user namespaces. A compatible model
server must be available for the agent to run. GUI tools also require Xvfb and X11;
DOCX/XLSX rendering requires LibreOffice, Poppler, and fonts.

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-modules.txt
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
Specify the address and model name of a running model server:

```sh
python project_state_agent.py --workdir /absolute/work --session-dir /absolute/session \
  --backend llama-cpp --base-url http://127.0.0.1:8897/v1 --model MODEL \
  --live 'Describe the task'
```

By default, `project_state_agent.py` loads 13 modules from
`examples/state-map.modules.json`; use `--modules` to select another manifest.
Modules are trusted Python code and are not sandboxed. `agent.py` runs a minimal
composition without this manifest. Both entry points support `--help`.
To use a managed llama-server, pass `--llama-server-config` and adapt the examples in
`examples/` to your system. The model and server are not included in the project.

The modules provide Shell, Project State and Map, images, background processes,
GUI, Browser, Expert, DOCX/XLSX, shell execution with model release, writing code
from reasoning, loop detection, and user input. Browser and Expert are inactive
by default and require configuration; see the [module capabilities](docs/loaded-capabilities.md)
and [module loader](docs/module-loader.md) documentation. Network access is controlled
separately for each applicable tool.

In an interactive terminal, Ctrl+Z requests a pause after the current turn, while
Ctrl+\ requests an urgent pause. Use `/multiline` and `/send` to enter several lines.
A paused session can resume with a user message. Streaming model messages to the user
and manual history editing have not been carried over.

## Interface and limitations

The [v2 interface](docs/event-interface.md) describes events, contributions, controls,
invocation context, observations, exclusive modes, and model release. Modules store
their state separately from the technical session; there is no shared crash-safe
transaction between them. An interrupted action with an unknown result blocks
automatic replay. To resume, use the same module configuration and retain both the
work directory and the session directory.

`shell_released` does not enforce the Project State review and context reset that
were mandatory in the previous implementation. Custom function packs and the history
bridge have not been carried over, and the old architecture is not maintained in
parallel. Compatibility with old sessions and configurations is not guaranteed.
See the [release notes](docs/release-0.7.0.md).
