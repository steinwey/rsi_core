"""Deterministic Schema 1 metadata, separate from editor document proposals."""
import hashlib
import re
import yaml
from yaml.tokens import AliasToken, AnchorToken

from rsi.skills.directory import INDEX_PATH, REFERENCE_PATH, validate_paths

MAX_INDEX_BYTES = 1024 * 1024
MIGRATED = re.compile(r'# ([^\n]{1,200})\n\nApplies when: ([^\n\[\]<>]{1,300})\n')
FIELDS = {'id', 'path', 'title', 'description', 'keywords', 'applies_when', 'exclude_when',
          'content_sha256', 'char_count', 'provenance', 'units'}


class UniqueLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in result:
                raise ValueError('Duplicate or non-string YAML key')
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def load_index(text):
    if not isinstance(text, str) or len(text.encode('utf-8')) > MAX_INDEX_BYTES:
        raise ValueError('Index exceeds size limit')
    try:
        if any(isinstance(t, (AliasToken, AnchorToken)) for t in yaml.scan(text)):
            raise ValueError('YAML aliases and anchors are not allowed')
        value = yaml.load(text, Loader=UniqueLoader)
    except (yaml.YAMLError, RecursionError) as exc:
        raise ValueError('Invalid index YAML') from exc
    if not isinstance(value, dict) or set(value) != {'schema_version', 'entries'} or type(value['schema_version']) is not int or value['schema_version'] != 1:
        raise ValueError('Unsupported index schema')
    if not isinstance(value['entries'], list) or len(value['entries']) > 1000:
        raise ValueError('Invalid index entries')
    return value


def _text(value, limit=2000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError('Invalid index text field')


def _strings(value, *, nonempty=False):
    if not isinstance(value, list) or len(value) > 64 or (nonempty and not value):
        raise ValueError('Invalid index string list')
    for item in value:
        _text(item, 500)


def validate_index(files):
    validate_paths(files)
    if any(not isinstance(text, str) for text in files.values()):
        raise ValueError('Skill contents must be text')
    data = load_index(files.get(INDEX_PATH))
    paths, ids = set(), set()
    refs = {p for p in files if p.startswith('references/')}
    if any(REFERENCE_PATH.fullmatch(p) is None for p in refs):
        raise ValueError('References must be single-level Markdown documents')
    for entry in data['entries']:
        if not isinstance(entry, dict) or set(entry) != FIELDS:
            raise ValueError('Invalid index entry fields')
        path, ident = entry['path'], entry['id']
        if not isinstance(path, str) or REFERENCE_PATH.fullmatch(path) is None or path not in refs:
            raise ValueError('Invalid index reference path')
        if not isinstance(ident, str) or re.fullmatch(r'[A-Za-z0-9_-]{1,128}', ident) is None:
            raise ValueError('Invalid index ID')
        if path in paths or ident in ids:
            raise ValueError('Duplicate index path or ID')
        paths.add(path)
        ids.add(ident)
        for key in ('title', 'description'):
            _text(entry[key])
        for key in ('keywords', 'applies_when', 'exclude_when'):
            _strings(entry[key], nonempty=key != 'exclude_when')
        if entry['content_sha256'] != sha(files[path]) or type(entry['char_count']) is not int or entry['char_count'] != len(files[path]):
            raise ValueError('Index content hash or char_count mismatch')
        provenance = entry['provenance']
        if not isinstance(provenance, list) or len(provenance) > 64:
            raise ValueError('Invalid provenance')
        for origin in provenance:
            if not isinstance(origin, dict):
                raise ValueError('Invalid provenance item')
            if origin == {'evidence_kind': 'legacy_unknown'}:
                continue
            if set(origin) != {'task_id', 'rollout', 'evidence_id', 'evidence_kind'} or origin['evidence_kind'] not in ('success', 'diagnostic_pass'):
                raise ValueError('Invalid provenance fields')
            _text(origin['task_id'], 200)
            if type(origin['rollout']) is not int or not 0 <= origin['rollout'] < 5 or re.fullmatch(r'[0-9a-f]{64}', str(origin['evidence_id'])) is None:
                raise ValueError('Invalid provenance evidence identity')
        units = entry['units']
        if not isinstance(units, list) or len(units) > 256:
            raise ValueError('Invalid index units')
        unit_ids = set()
        for unit in units:
            if not isinstance(unit, dict) or set(unit) != {'id', 'heading_path', 'content_sha256', 'self_contained'}:
                raise ValueError('Invalid unit fields')
            _text(unit['id'], 128)
            _strings(unit['heading_path'], nonempty=True)
            if unit['id'] in unit_ids or re.fullmatch(r'[0-9a-f]{64}', str(unit['content_sha256'])) is None or type(unit['self_contained']) is not bool:
                raise ValueError('Invalid unit identity')
            unit_ids.add(unit['id'])
    if paths != refs:
        raise ValueError('Index paths must equal references; orphan reference')
    return data


def build_index(files, previous=None, *, source_files=None):
    """No inferred success provenance; unchanged files retain their metadata.

    The default is P0-compatible. P1 can supply source_files to preserve existing
    origins on verified append-only changes. LLM output is never a managed edit.
    """
    old = {e['path']: e for e in previous['entries']} if previous else {}
    entries = []
    for path in sorted(p for p in files if REFERENCE_PATH.fullmatch(p)):
        text = files[path]
        if path in old and old[path]['content_sha256'] == sha(text):
            entry = dict(old[path])
        else:
            entry = {'id': 'ref-' + sha(path)[:24], 'path': path,
                     'title': path.removeprefix('references/').removesuffix('.md'),
                     'description': 'Legacy reference; applicability has not been reviewed.',
                     'keywords': [path.removeprefix('references/').removesuffix('.md')],
                     'applies_when': ['Consult the document for its applicability conditions.'],
                     'exclude_when': [], 'provenance': [{'evidence_kind': 'legacy_unknown'}], 'units': []}
            migrated = MIGRATED.match(text)
            if migrated:
                entry.update(title=migrated[1], applies_when=[migrated[2]],
                             description='Specific guidance migrated verbatim from SKILL.md.',
                             keywords=list(dict.fromkeys([entry['keywords'][0], migrated[1].lower()])))
        if (source_files and path in old and path in source_files
                and old[path]['content_sha256'] == sha(source_files[path])
                and text != source_files[path] and text.startswith(source_files[path])):
            # Append-only extensions retain known origins plus an explicit unknown
            # origin for the new material; no PASS is inferred for new text.
            entry['provenance'] = list(old[path]['provenance'])
            unknown = {'evidence_kind': 'legacy_unknown'}
            if unknown not in entry['provenance']:
                entry['provenance'].append(unknown)
        entry.update(content_sha256=sha(text), char_count=len(text))
        entries.append(entry)
    value = {'schema_version': 1, 'entries': entries}
    serialized = yaml.safe_dump(value, allow_unicode=True, sort_keys=True, width=1000)
    validate_index(files | {INDEX_PATH: serialized})
    return serialized
