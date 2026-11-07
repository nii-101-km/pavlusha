#!/usr/bin/env python3
"""Stop a local llama-server previously started with --keep-server."""
import argparse
import sys

from pavlusha_agent.core import AgentError
from pavlusha_agent.persistent_server import server_lock, state_key, state_root, stop_record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', help='Server URL, required only when several saved servers exist')
    args = parser.parse_args(argv)
    try:
        root = state_root()
        if args.base_url:
            key = state_key(args.base_url)
            paths = [root / (key + '.json')]
        else:
            paths = list(root.glob('*.json'))
            if not paths:
                print('No persistent Pavlusha servers found.')
                return 0
            if len(paths) != 1:
                raise AgentError('multiple persistent servers found; specify --base-url')
            key = paths[0].stem
        with server_lock(root, key):
            path = paths[0]
            if not path.exists():
                raise AgentError('no persistent server recorded for this address')
            record = stop_record(root, path)
            print('Stopped persistent Pavlusha server: ' + record['base_url'])
        return 0
    except (AgentError, OSError, ValueError) as exc:
        print('ERROR: ' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
