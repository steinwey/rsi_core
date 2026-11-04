"""Skill-only file policy; legacy collection and hashes remain unchanged."""
import os
import re
from pathlib import Path

from rsi.core.common import file_map

INDEX_PATH = 'routing/index.yaml'
REFERENCE_PATH = re.compile(r'references/[A-Za-z0-9_][A-Za-z0-9_.-]*\.md')


def editable(path):
    return isinstance(path, str) and (path == 'SKILL.md' or REFERENCE_PATH.fullmatch(path) is not None)


def immutable(files):
    return {p: text for p, text in files.items() if not editable(p) and p != INDEX_PATH}


def validate_path(path):
    if not isinstance(path, str) or not path or '\\' in path or path.startswith('/'):
        raise ValueError(f'Illegal Skill path: {path!r}')
    for part in path.split('/'):
        if (part in ('', '.', '..') or part[-1:] in ('.', ' ')
                or re.search(r'[<>:"|?*\x00-\x1f]', part)
                or re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', part)):
            raise ValueError(f'Illegal Skill path: {path!r}')


def validate_paths(paths):
    paths = list(paths)
    if any(not isinstance(path, str) for path in paths):
        raise ValueError('Skill paths must be strings')
    seen = {}
    for path in sorted(paths):
        validate_path(path)
        parts = path.split('/')
        for n in range(1, len(parts) + 1):
            prefix = '/'.join(parts[:n])
            folded = prefix.casefold()
            if folded in seen and seen[folded] != prefix:
                raise ValueError(f'Case collision: {prefix}')
            seen[folded] = prefix
    names = set(paths)
    if any('/'.join(p.split('/')[:i]) in names for p in names for i in range(1, len(p.split('/')))):
        raise ValueError('File/directory collision')


def reject_links(path):
    for parent in (Path(path), *Path(path).parents):
        if parent.is_symlink() or (hasattr(parent, 'is_junction') and parent.is_junction()):
            raise ValueError(f'Skill symlink/junction: {parent}')


def read_skill_directory(directory, *, directory_schema=0):
    if directory_schema not in (0, 1) or type(directory_schema) is not int:
        raise ValueError('Unsupported Skill directory schema')
    root = Path(directory)
    # Reject redirected roots, including Windows junctions, before traversal.
    reject_links(root)
    paths = []
    for current, dirs, names in os.walk(root, followlinks=False):
        for name in dirs + names:
            p = Path(current) / name
            if p.is_symlink() or (hasattr(p, 'is_junction') and p.is_junction()):
                raise ValueError(f'Skill symlink/junction: {p}')
        for name in names:
            p = Path(current) / name
            rel = p.relative_to(root).as_posix()
            if '__pycache__' in p.parts:
                continue
            if directory_schema and p.suffix not in ('.md', '.py') and rel != INDEX_PATH:
                raise ValueError(f'Unsupported Skill file: {rel}')
            paths.append(rel)
    validate_paths(paths)
    if not directory_schema:
        return file_map(root)
    return {p: (root / p).read_text(encoding='utf-8') for p in sorted(paths)}


def directory_schema(config):
    block = config.get('skill_directory', {})
    if not isinstance(block, dict) or block.keys() - {'schema'}:
        raise ValueError('Invalid skill_directory configuration')
    schema = block.get('schema', 0)
    if type(schema) is not int or schema not in (0, 1):
        raise ValueError('skill_directory.schema must be 0 or 1')
    for key in ('reference_maintenance', 'skill_routing'):
        value = config.get(key, {})
        if not isinstance(value, dict) or value.keys() - {'enabled'} or type(value.get('enabled', False)) is not bool:
            raise ValueError(f'Invalid {key} configuration')
        if value.get('enabled', False):
            raise ValueError(f'{key} is not implemented in P0')
    from rsi.skills.maintenance import settings
    maintenance = settings(config)
    if maintenance['enabled'] and schema != 1:
        raise ValueError('Maintenance requires the P0 directory schema 1')
    if schema and config.get('skill_update_method', 'trace2skill') != 'trace2skill':
        raise ValueError('Managed Skill directories require Trace2Skill')
    return schema
