#!/usr/bin/env python3
"""Experimental composition using the unified event contract."""
from contextlib import ExitStack
import argparse
import math
import os
from pathlib import Path
import sys

from pavlusha_agent.adapters import ChatProvider
from pavlusha_agent.assembly_config import load_assembly
from pavlusha_agent import __version__
from pavlusha_agent.core import AgentError
from pavlusha_agent.llama_server import ManagedLlamaServer
from pavlusha_agent.llama_server_proxy import AttachedLlamaServer, LlamaServerControl
from pavlusha_agent.live import LiveConsoleRenderer
from pavlusha_agent.runtime import Runtime
from pavlusha_agent.terminal_input import TerminalInput
from pavlusha_modules.loader import load_modules


def module_context(args, workdir, session_dir, kv_storage_dir=None):
    """Expose inherited launch settings to modules without sharing mutable objects."""
    names = ('base_url', 'backend', 'model', 'api_timeout', 'temperature',
             'max_tokens', 'worker_context_budget', 'reasoning_effort', 'max_steps',
             'history_high', 'history_context_high', 'output_limit', 'command_timeout',
             'network', 'review_every', 'live', 'live_format', 'interactive')
    context = {name: getattr(args, name) for name in names}
    context.update(workdir=str(workdir), session_dir=str(session_dir), kv_storage_dir=kv_storage_dir,
                   server_control_path=getattr(args, 'server_control_path', None),
                   modules_path=str(args.modules.expanduser().resolve()),
                   llama_server_config=(str(Path(args.llama_server_config).expanduser().resolve())
                                        if isinstance(args.llama_server_config, (str, Path)) else None))
    return context


def main():
    parser = argparse.ArgumentParser(description='Pavlusha assembly from a configuration file')
    parser.add_argument('--version', action='version', version=f'Pavlusha {__version__}')
    parser.add_argument('--config', type=Path, required=True, help='Assembly configuration JSON')
    parser.add_argument('--workdir', required=True)
    parser.add_argument('--session-dir', required=True)
    parser.add_argument('--user-input', help='Reply to a paused experimental session')
    parser.add_argument('--server-control-path', help=argparse.SUPPRESS)
    parser.add_argument('--kv-storage-dir', help=argparse.SUPPRESS)
    parser.add_argument('task', nargs='*')
    launch = parser.parse_args()
    try:
        task = ' '.join(launch.task).strip() if launch.task else sys.stdin.read().strip()
        if not task:
            raise AgentError('task is empty')
        args = load_assembly(launch.config, workdir=launch.workdir, session_dir=launch.session_dir, task=task)
        args.user_input = launch.user_input
        args.server_control_path = launch.server_control_path
        args.kv_storage_dir = launch.kv_storage_dir
        args.enable_server_control = getattr(args, 'enable_server_control', False)
        if args.server_control_path:
            # A delegated child attaches to its parent's server, never starts another.
            args.llama_server_config = None
            args.enable_server_control = False
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
        with ExitStack() as cleanup:
            if args.server_control_path and args.llama_server_config:
                raise AgentError('server-control-path and llama-server-config are mutually exclusive')
            if args.enable_server_control and not args.llama_server_config:
                raise AgentError('enable-server-control requires a managed llama-server')
            server = (ManagedLlamaServer(args.llama_server_config, args.base_url,
                session_dir.with_suffix('.server.log'), args.api_timeout)
                if args.llama_server_config else
                AttachedLlamaServer(args.base_url, args.server_control_path, args.api_timeout)
                if args.server_control_path else None)
            kv_storage_dir = args.kv_storage_dir if args.server_control_path else None
            if args.llama_server_config:
                cleanup.callback(server.__exit__, None, None, None)
                kv_storage_dir = server.prepare_kv_directory()
                if args.enable_server_control and not kv_storage_dir:
                    raise AgentError('enable-server-control requires preserve_kv')
                if args.enable_server_control:
                    args.server_control_path = str(Path(kv_storage_dir) / 'control.sock')
            extensions = load_modules(args.modules, context=module_context(args, workdir, session_dir, kv_storage_dir))
            interactive = sys.stdin.isatty() if args.interactive is None else args.interactive
            if interactive and not sys.stdin.isatty():
                raise AgentError('interactive mode requires terminal stdin')
            if interactive and not any(callable(getattr(m, 'bind_interaction', None)) for m in extensions):
                raise AgentError('interactive mode requires a module that handles live interaction')
            # Validate names before starting the model server.
            from pavlusha_agent.extensions import EventHost
            EventHost(extensions)
            if args.llama_server_config:
                server.start()
                if args.server_control_path:
                    cleanup.enter_context(LlamaServerControl(server, args.server_control_path))
            provider = ChatProvider(args.base_url, args.model, os.environ.get(args.api_key_env, ''), args.api_timeout,
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
