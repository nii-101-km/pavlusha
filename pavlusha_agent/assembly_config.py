"""Read a single assembly configuration and validate runtime settings."""
import argparse
import json
from pathlib import Path

from .cli import build_parser
from .core import AgentError


def load_assembly(path, *, workdir, session_dir, task):
    path = Path(path).expanduser().resolve()
    try:
        config = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise AgentError(f'cannot read assembly config {path}: {exc}') from exc
    if (type(config) is not dict or not {'runtime', 'modules'} <= set(config)
            or set(config) - {'runtime', 'modules', 'module_base_dir'}
            or type(config['runtime']) is not dict or type(config['modules']) is not list
            or ('module_base_dir' in config and not isinstance(config['module_base_dir'], str))):
        raise AgentError('assembly config requires runtime object and modules array')
    runtime = dict(config['runtime'])
    inline_server = runtime.pop('llama_server', None)
    if inline_server is not None and (type(inline_server) is not dict or not inline_server or 'llama_server_config' in runtime):
        raise AgentError('runtime.llama_server must be an object and cannot be combined with llama_server_config')
    parser = build_parser()
    parser.add_argument('--review-every', type=int, default=10)
    parser.add_argument('--interactive', action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument('--live-format', choices=('human', 'json'), default='human')
    parser.add_argument('--enable-server-control', action='store_true')
    actions = {a.dest: a for a in parser._actions if a.dest not in ('help', 'version', 'task', 'workdir', 'session_dir')}
    unknown = set(runtime) - set(actions)
    if unknown:
        raise AgentError('unknown assembly runtime settings: ' + ', '.join(sorted(unknown)))
    if not isinstance(runtime.get('model'), str) or not runtime['model'].strip():
        raise AgentError('assembly runtime requires a nonempty model')
    argv = ['--workdir', str(workdir), '--session-dir', str(session_dir)]
    for name, value in runtime.items():
        action = actions[name]
        option = '--' + name.replace('_', '-')
        if isinstance(action, argparse.BooleanOptionalAction):
            if type(value) is not bool:
                raise AgentError(f'assembly runtime {name} must be boolean')
            argv.append(option if value else '--no-' + name.replace('_', '-'))
        elif isinstance(action, argparse._StoreTrueAction):
            if type(value) is not bool:
                raise AgentError(f'assembly runtime {name} must be boolean')
            if value:
                argv.append(option)
        else:
            if value is None or isinstance(value, (dict, list, bool)) or not isinstance(value, (str, int, float)):
                raise AgentError(f'invalid assembly runtime setting {name}')
            if name == 'llama_server_config':
                value = str((path.parent / Path(str(value)).expanduser()).resolve())
            argv.extend([option, str(value)])
    argv.extend(['--', task])
    # argparse normally exits the process on malformed values; convert it to an agent error.
    def config_error(message):
        raise AgentError(f'invalid assembly runtime settings in {path}: {message}')
    parser.error = config_error
    args = parser.parse_args(argv)
    if inline_server is not None:
        args.llama_server_config = inline_server
    args.modules = path
    args.config = path
    return args
