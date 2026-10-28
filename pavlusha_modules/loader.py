"""Load trusted extensions; resource startup/cleanup remains owned by EventHost."""
import copy
import importlib
import json
from pathlib import Path

from pavlusha_agent.core import AgentError


def load_modules(path, *, context=None):
    """Manifest: {"modules": [{"factory": "package:create_extension", "settings": {}}]}.

    Factories must only construct handlers. Allocate resources on session_started,
    where the event host can clean up partial initialization failures.
    """
    path = Path(path).expanduser().resolve()
    try:
        manifest = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise AgentError(f'cannot read module manifest {path}: {exc}') from exc
    if (type(manifest) is not dict or set(manifest) != {'modules'}
            or type(manifest['modules']) is not list):
        raise AgentError('module manifest requires a modules array')
    entries = manifest['modules']
    for entry in entries:
        if (type(entry) is not dict or set(entry) - {'factory', 'settings'}
                or type(entry.get('factory')) is not str
                or type(entry.get('settings', {})) is not dict):
            raise AgentError('module entry requires factory and optional settings object')
        parts = entry['factory'].split(':')
        if (len(parts) != 2 or not all(p.isidentifier() for p in parts[0].split('.'))
                or not parts[1].isidentifier()):
            raise AgentError('module factory must be package.module:function')
    loaded, names = [], set()
    for entry in entries:
        target = entry['factory']
        module_name, factory_name = target.split(':')
        try:
            factory = getattr(importlib.import_module(module_name), factory_name)
            module_context = copy.deepcopy(context or {})
            module_context['config_dir'] = str(path.parent)
            extension = factory(settings=copy.deepcopy(entry.get('settings', {})),
                                context=module_context)
        except Exception as exc:
            raise AgentError(f'cannot construct extension {target}: {exc}') from exc
        name = getattr(extension, 'name', None)
        if (type(name) is not str or not name.isidentifier()
                or not callable(getattr(extension, 'on_event', None))):
            raise AgentError(f'invalid extension returned by {target}')
        if name in names:
            raise AgentError('duplicate extension name: ' + name)
        names.add(name)
        loaded.append(extension)
    return loaded
