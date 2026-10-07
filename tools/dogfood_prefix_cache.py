"""Historical prefix-cache probe for the retired drop/compaction protocol.

This is not a current-runtime regression test: drop_context was removed, and
the maintenance CLI options below are accepted only for compatibility.
"""
import argparse
from pathlib import Path
import subprocess
import sys


def run():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--log', type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    offset = args.log.stat().st_size if args.log else 0
    task = (
        "This is a tiny runtime smoke test. Execute exactly this sequence: "
        "1. Use a shell action to run printf 'prefix-cache smoke observation\\n'. "
        "2. Use drop_context to remove the resulting W0001 observation, intent 'smoke test cleanup'. "
        "3. Use a shell action to run printf 'prefix-cache smoke verification\\n'. "
        "4. Finish with summary 'prefix-cache smoke done'. Do not inspect files or do any other work."
    )
    with (output / 'live.txt').open('w') as console:
        status = subprocess.run([
            sys.executable, str(Path(__file__).resolve().parents[1] / 'agent.py'),
            '--workdir', str(output / 'work'), '--state-dir', str(output / 'state'),
            '--no-interactive', '--reset-state', '--model', 'qwen/qwen3.8-27b', '--live', '--verbose',
            '--worker-context-control', 'drop', '--state-cycle-after', '2',
            '--state-keep', '1', '--max-steps', '6', task,
        ], stdout=console, stderr=console).returncode
    if args.log:
        with args.log.open('rb') as backend:
            backend.seek(offset)
            (output / 'backend.log').write_bytes(backend.read())
    print(f'Runtime exit: {status}; output: {output}')
    return status


if __name__ == '__main__':
    raise SystemExit(run())
