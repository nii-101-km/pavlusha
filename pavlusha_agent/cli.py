"""Isolated bare-kernel CLI; deliberately no legacy project/module switches."""
import argparse
from contextlib import nullcontext
import math
import os
from pathlib import Path
import sys

from .adapters import ChatProvider
from .basic_host import BasicHost
from .core import AgentError
from .llama_server import ManagedLlamaServer
from .runtime import Runtime
from . import __version__


def build_parser():
    parser = argparse.ArgumentParser(description='Experimental bare Pavlusha — separate copy only')
    parser.add_argument('--version', action='version', version=f'Pavlusha {__version__}')
    parser.add_argument('task', nargs='*')
    parser.add_argument('--workdir', required=True)
    parser.add_argument('--session-dir', required=True)
    parser.add_argument('--base-url', default='http://127.0.0.1:8877/v1')
    parser.add_argument('--backend', choices=('llama-cpp', 'lm-studio', 'chat-completions', 'perplexity-agent'), default='llama-cpp')
    parser.add_argument('--api-key-env', default='PAVLUSHA_API_KEY', help='Environment variable containing the model API key')
    parser.add_argument('--model', required=True)
    parser.add_argument('--llama-server-config')
    parser.add_argument('--api-timeout', type=float, default=300)
    parser.add_argument('--worker-context-budget', type=int, default=16384)
    parser.add_argument('--max-tokens', type=int, default=4096)
    parser.add_argument('--temperature', type=float, default=.1)
    parser.add_argument('--reasoning-effort', default='low')
    parser.add_argument('--max-steps', type=int, default=40)
    parser.add_argument('--history-high', type=int, default=200)
    parser.add_argument('--history-context-high', type=float, default=.85)
    parser.add_argument('--output-limit', type=int, default=24000)
    parser.add_argument('--command-timeout', type=int, default=120)
    parser.add_argument('--network', action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument('--live', action='store_true')
    return parser


def main():
    try:
        args = build_parser().parse_args()
        task = ' '.join(args.task).strip() if args.task else sys.stdin.read().strip()
        if not task:
            raise AgentError('task is empty')
        for name in ('api_timeout', 'worker_context_budget', 'max_tokens', 'max_steps',
                     'history_high', 'output_limit', 'command_timeout'):
            value = getattr(args, name)
            if not math.isfinite(value) or value <= 0:
                raise AgentError(name + ' must be positive and finite')
        if not 0 < args.history_context_high < 1:
            raise AgentError('history-context-high must be between zero and one')
        workdir = Path(args.workdir).expanduser().resolve()
        session_dir = Path(args.session_dir).expanduser().resolve()
        if session_dir.is_relative_to(workdir) or workdir.is_relative_to(session_dir):
            raise AgentError('workdir and session-dir must be separate, non-nested directories')
        workdir.mkdir(parents=True, exist_ok=True)
        owned = ManagedLlamaServer(args.llama_server_config, args.base_url,
            session_dir.with_suffix('.server.log'), args.api_timeout) if args.llama_server_config else nullcontext()
        with owned as server:
            provider = ChatProvider(args.base_url, args.model, os.environ.get(args.api_key_env, ''), args.api_timeout, args.temperature,
                                    args.max_tokens, llama_server=server, backend=args.backend)
            runtime = Runtime(provider, task=task, session_dir=session_dir,
                extensions=[BasicHost(workdir, network=args.network, timeout=args.command_timeout,
                                      output_limit=args.output_limit)],
                max_steps=args.max_steps, history_high=args.history_high,
                context_capacity=args.worker_context_budget, context_fraction=args.history_context_high,
                output_limit=args.output_limit, reasoning_effort=args.reasoning_effort, live=args.live)
            print(runtime.run())
        return 0
    except KeyboardInterrupt:
        print('Interrupted.', file=sys.stderr)
        return 130
    except (AgentError, OSError, ValueError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 2
