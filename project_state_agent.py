#!/usr/bin/env python3
"""Experimental composition using the unified event contract."""
from contextlib import nullcontext
import argparse
import math
from pathlib import Path
import sys

from pavlusha_agent.adapters import ChatProvider
from pavlusha_agent.cli import build_parser
from pavlusha_agent.core import AgentError
from pavlusha_agent.llama_server import ManagedLlamaServer
from pavlusha_agent.live import LiveConsoleRenderer
from pavlusha_agent.runtime import Runtime
from pavlusha_agent.terminal_input import TerminalInput
from pavlusha_modules.loader import load_modules


def main():
    parser = build_parser()
    parser.description = 'Experimental Project State and Project Map composition using module API v2'
    parser.add_argument('--review-every', type=int, default=10)
    parser.add_argument('--modules', type=Path,
        default=Path(__file__).resolve().parent / 'examples/state-map.modules.json')
    parser.add_argument('--user-input', help='Reply to a paused experimental session')
    parser.add_argument('--interactive', action=argparse.BooleanOptionalAction, default=None,
                        help='Enable live terminal pause and input (default: terminal stdin)')
    parser.add_argument('--live-format', choices=('human', 'json'), default='human',
                        help='Live progress format (default: human)')
    args = parser.parse_args()
    try:
        task = ' '.join(args.task).strip() if args.task else sys.stdin.read().strip()
        if not task:
            raise AgentError('task is empty')
        for name in ('api_timeout', 'worker_context_budget', 'max_tokens', 'max_steps',
                     'history_high', 'output_limit', 'command_timeout'):
            if not math.isfinite(getattr(args, name)) or getattr(args, name) <= 0:
                raise AgentError(name + ' must be positive and finite')
        if not 0 < args.history_context_high < 1 or args.review_every < 0:
            raise AgentError('invalid context fraction or review interval')
        workdir = Path(args.workdir).expanduser().resolve()
        session_dir = Path(args.session_dir).expanduser().resolve()
        if session_dir.is_relative_to(workdir) or workdir.is_relative_to(session_dir):
            raise AgentError('workdir and session-dir must be separate, non-nested directories')
        workdir.mkdir(parents=True, exist_ok=True)
        extensions = load_modules(args.modules, context={
            'workdir': str(workdir), 'review_every': args.review_every,
            'network': args.network, 'command_timeout': args.command_timeout,
            'output_limit': args.output_limit})
        interactive = sys.stdin.isatty() if args.interactive is None else args.interactive
        if interactive and not sys.stdin.isatty():
            raise AgentError('interactive mode requires terminal stdin')
        if interactive and not any(callable(getattr(m, 'bind_interaction', None)) for m in extensions):
            raise AgentError('interactive mode requires a module that handles live interaction')
        # Validate names before starting the model server.
        from pavlusha_agent.extensions import EventHost
        EventHost(extensions)
        lifecycle = (ManagedLlamaServer(args.llama_server_config, args.base_url,
            session_dir.with_suffix('.server.log'), args.api_timeout)
            if args.llama_server_config else nullcontext())
        with lifecycle as server:
            provider = ChatProvider(args.base_url, args.model, '', args.api_timeout,
                args.temperature, args.max_tokens, llama_server=server, backend=args.backend)
            live = args.live or interactive
            renderer = LiveConsoleRenderer(stream=sys.stdout) if live and args.live_format == 'human' else None
            if renderer is not None:
                renderer.start(model=args.model, task=task, max_steps=args.max_steps,
                               context=args.worker_context_budget)
            runtime = Runtime(provider, task=task, session_dir=session_dir,
                extensions=extensions,
                max_steps=args.max_steps, history_high=args.history_high,
                context_capacity=args.worker_context_budget, context_fraction=args.history_context_high,
                output_limit=args.output_limit, reasoning_effort=args.reasoning_effort, live=live,
                user_input=args.user_input, interaction_ui=TerminalInput() if interactive else None,
                renderer=renderer)
            summary = runtime.run()
            if renderer is not None and runtime.session.state['status'] == 'finished':
                renderer.complete(summary)
            else:
                print(summary)
        return 0
    except KeyboardInterrupt:
        print('Interrupted.', file=sys.stderr)
        return 130
    except (AgentError, OSError, ValueError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
