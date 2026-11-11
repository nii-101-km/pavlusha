#!/bin/sh
# Run the assembly with its installed Python dependencies.
set -eu
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
venv_dir=${PAVLUSHA_VENV:-${VIRTUAL_ENV:-"$project_dir/.venv"}}
python_bin=$venv_dir/bin/python

if [ ! -x "$python_bin" ]; then
    printf '%s\n' "Python environment not found: $venv_dir" \
        "Create it with: python3 -m venv \"$project_dir/.venv\"" \
        "Then install dependencies: \"$project_dir/.venv/bin/python\" -m pip install -r \"$project_dir/requirements.txt\"" \
        "Or set PAVLUSHA_VENV to an existing environment." >&2
    exit 1
fi

if ! "$python_bin" -c 'import jsonschema, tree_sitter, tree_sitter_python, rich, PIL, Xlib, websocket' >/dev/null 2>&1; then
    printf '%s\n' "Python dependencies are missing from: $venv_dir" \
        "Install them with: \"$python_bin\" -m pip install -r \"$project_dir/requirements.txt\"" >&2
    exit 1
fi

exec "$python_bin" "$project_dir/project_state_agent.py" "$@"
