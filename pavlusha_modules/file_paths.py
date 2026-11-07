"""Common bounded paths for optional file-oriented modules."""
from pathlib import Path


def work_file(root, value, suffix):
    if not isinstance(value,str) or not value or len(value)>512 or '\0' in value:
        raise ValueError('Invalid path')
    rel=Path(value)
    if rel.is_absolute() or '..' in rel.parts:
        raise ValueError('Use paths relative to workdir')
    raw=root/rel
    resolved=raw.resolve()
    if resolved == root or not resolved.is_relative_to(root) or resolved.suffix.lower()!=suffix:
        raise ValueError('Path must point to a '+suffix+' file inside workdir')
    if raw.is_symlink():
        raise ValueError('Symlink files are not supported')
    return resolved
