"""Bounded MigrationPlan validation and deterministic, lossless application."""
import posixpath
import re
from rsi.core.common import digest
from rsi.skills.directory import REFERENCE_PATH, validate_paths
from rsi.skills.markdown_units import (parse_experience_units, parse_units, rebase_links, stub, sha,
                                       canonical, UnsafeMarkdown)

SCHEMA = 2
PLAN_FIELDS = {'schema', 'input_hash', 'classifications', 'operations'}
CLASS_FIELDS = {'unit_id', 'class', 'topic', 'reason'}
CLASSES = {'core', 'specific', 'mixed', 'uncertain'}
# The model only decides where a unit goes; text, spans and hashes come from the program.
OP_FIELDS = {'unit_id', 'target', 'topic', 'applies_when', 'requires', 'reason'}
TOPIC = re.compile(r'[a-z0-9][a-z0-9-]{0,47}')
MAX_UNITS = 256


def _reason(value):
    return isinstance(value, str) and value.strip() and len(value) <= 2000


def applies_when_valid(value):
    return (isinstance(value, str) and value == value.strip() and 0 < len(value) <= 300
            and not re.search(r'[\n\r\[\]<>]', value))


def title_for(topic):
    return ' '.join(word.capitalize() for word in topic.split('-') if word) or topic


def header(title, applies_when):
    return f'# {title}\n\nApplies when: {applies_when}\n'


MARKER = re.compile(r'^( {0,3}(?:[-*+]|\d{1,3}[A-Za-z]?[.)]))[ \t]+')


def navigation(unit, applies_when, title, target):
    """One line in place of each moved unit; list markers and numbering stay in order."""
    body = unit.text.rstrip('\n')
    marker = MARKER.match(body) if unit.kind == 'item' else None
    prefix = marker[1] + ' ' if marker else ''
    return f'{prefix}Specific guidance ({applies_when}): [{title}]({target})\n' + unit.text[len(body) + 1:]


def moved_block(unit, target, files):
    try:
        return canonical(rebase_links(unit.text.strip('\n') + '\n', 'SKILL.md', target, files))
    except UnsafeMarkdown as exc:
        raise UnsafeMarkdown(f'{exc} (unit {unit.id}: {unit.text[:80]!r})') from exc


def _sibling(target, used):
    stem = target.removesuffix('.md')
    for n in range(2, 1000):
        path = f'{stem}-{n}.md'
        if path not in used:
            return path
    raise UnsafeMarkdown('No free migration overflow destination')


def apply_plan(files, plan, max_reference_chars, *, require_shrink=True, min_new_reference_chars=0):
    """Validate a schema-2 plan over experience units and move them verbatim.

    Returns (files, coverage, classifications, issues). Each operation is
    checked on its own: an invalid one is dropped and reported in `issues`
    while the valid ones still apply. Unclassified units count as uncertain
    and stay. An empty result is a valid "nothing worth moving" decision
    unless the caller requires shrinkage.
    """
    if (not isinstance(plan, dict) or set(plan) != PLAN_FIELDS or type(plan['schema']) is not int
            or plan['schema'] != SCHEMA or plan['input_hash'] != digest(files)):
        raise UnsafeMarkdown('Invalid MigrationPlan identity')
    units = {u.id: u for u in parse_experience_units(files['SKILL.md'])}
    movable = {i for i, u in units.items() if not u.protected}
    classes, operations = plan['classifications'], plan['operations']
    if not isinstance(classes, list) or len(classes) > MAX_UNITS:
        raise UnsafeMarkdown('Invalid MigrationPlan classifications')
    if not isinstance(operations, list) or len(operations) > 128:
        raise UnsafeMarkdown('MigrationPlan requires bounded operations')
    labels, issues = {}, []
    def drop(unit_id, error):
        issues.append({'unit_id': unit_id if isinstance(unit_id, str) else None, 'error': error})
    for item in classes:
        if (not isinstance(item, dict) or not CLASS_FIELDS <= item.keys() or item['unit_id'] not in movable
                or item['unit_id'] in labels or item['class'] not in CLASSES
                or not isinstance(item['topic'], str)
                or (item['topic'] and TOPIC.fullmatch(item['topic']) is None)
                or (item['class'] == 'specific' and not item['topic'])):
            drop(item.get('unit_id') if isinstance(item, dict) else None, 'invalid classification; treated as uncertain')
            continue
        labels[item['unit_id']] = {k: item[k] for k in CLASS_FIELDS} | (
            {'topic': ''} if item['class'] in ('core', 'uncertain') else {})
    for missing in sorted(movable - labels.keys()):
        labels[missing] = {'unit_id': missing, 'class': 'uncertain', 'topic': '', 'reason': 'not classified'}
    selected, topics = {}, {}
    for op in operations:
        unit = units.get(op.get('unit_id')) if isinstance(op, dict) and isinstance(op.get('unit_id'), str) else None
        ident = op.get('unit_id') if isinstance(op, dict) else None
        if not isinstance(op, dict) or not OP_FIELDS <= op.keys():
            drop(ident, 'missing operation fields')
        elif unit is None or unit.id in selected or unit.protected:
            drop(ident, 'unknown, duplicate or protected unit')
        elif labels[unit.id]['class'] != 'specific' or op['topic'] != labels[unit.id]['topic']:
            drop(unit.id, 'unit is not classified specific under this topic')
        elif not applies_when_valid(op['applies_when']):
            drop(unit.id, 'applies_when must be one line of at most 300 characters without brackets')
        elif (not isinstance(op['requires'], list) or len(op['requires']) > 32
                or any(r not in units or r == unit.id for r in op['requires'])):
            drop(unit.id, 'requires lists unknown units')
        elif not isinstance(op['target'], str) or REFERENCE_PATH.fullmatch(op['target']) is None:
            drop(unit.id, 'illegal target path')
        elif topics.setdefault(op['topic'], op['target']) != op['target']:
            drop(unit.id, 'one topic must use one target')
        else:
            selected[unit.id] = (unit, op)
    mixed_new = {t for t in topics.values() if t not in files and sum(v == t for v in topics.values()) > 1}
    for unit_id, (unit, op) in list(selected.items()):
        if op['target'] in mixed_new:
            del selected[unit_id]
            drop(unit_id, 'a new reference cannot mix topics')
    changed = True
    while changed:
        changed = False
        for unit_id, (unit, op) in list(selected.items()):
            if any(r in selected and selected[r][1]['target'] != op['target'] for r in op['requires']):
                del selected[unit_id]
                drop(unit_id, 'dependent units must stay or move together')
                changed = True
    sizes = {}
    for unit_id, (unit, op) in list(selected.items()):
        try:
            sizes[unit_id] = len(moved_block(unit, op['target'], files))
        except UnsafeMarkdown as exc:
            del selected[unit_id]
            drop(unit_id, str(exc))
    new_totals = {}
    for unit_id, (unit, op) in selected.items():
        if op['target'] not in files:
            new_totals[op['target']] = new_totals.get(op['target'], 0) + sizes[unit_id] + 1
    for unit_id, (unit, op) in list(selected.items()):
        total = new_totals.get(op['target'])
        if total is not None and total < min_new_reference_chars:
            del selected[unit_id]
            drop(unit_id, f'new reference {op["target"]} would hold only {total} characters '
                          f'(minimum {min_new_reference_chars}); use a broader topic shared with '
                          'related units or an existing reference')
    if not selected:
        if require_shrink:
            raise UnsafeMarkdown('Migration made no progress' + (': ' + str(issues[:8]) if issues else ''))
        return dict(files), [], list(labels.values()), issues
    validate_paths([*files, *(op['target'] for _, op in selected.values() if op['target'] not in files)])
    result, coverage, used = dict(files), [], set(files)
    current = {}
    for unit, op in sorted(selected.values(), key=lambda pair: pair[0].start):
        planned = op['target']
        target = current.get(planned, planned)
        title = title_for(op['topic']) if planned not in files else posixpath.splitext(posixpath.basename(planned))[0]
        block = moved_block(unit, target, files)
        previous = result.get(target, '')
        head = '' if target in result else header(title, op['applies_when'])
        joined = (previous + '\n' + block) if previous else head + '\n' + block
        if len(joined) > max_reference_chars:
            sibling = _sibling(planned, used)
            head = header(title, op['applies_when'])
            block = moved_block(unit, sibling, files)
            if len(head + '\n' + block) > max_reference_chars:
                drop(unit.id, 'unit exceeds the reference size limit')
                continue
            target, previous, joined = sibling, '', head + '\n' + block
            current[planned] = target
        used.add(target)
        result[target] = joined
        nav = navigation(unit, op['applies_when'], title, target)
        coverage.append({'kind': 'experience', 'source': 'SKILL.md', 'unit_id': unit.id,
                         'start': unit.start, 'end': unit.end, 'source_sha256': unit.content_sha256,
                         'fingerprint': unit.fingerprint, 'target': target, 'header': head,
                         'target_start': len(joined) - len(block), 'target_end': len(joined),
                         'target_sha256': sha(block), 'topic': op['topic'], 'title': title,
                         'applies_when': op['applies_when'], 'navigation': nav,
                         'method': 'verbatim_with_rebased_links'})
    root = files['SKILL.md']
    for record in sorted(coverage, key=lambda r: r['start'], reverse=True):
        root = root[:record['start']] + record['navigation'] + root[record['end']:]
    result['SKILL.md'] = canonical(root)
    if coverage:
        validate_coverage(files, result, coverage)
    if require_shrink and len(result['SKILL.md']) >= len(files['SKILL.md']):
        raise UnsafeMarkdown('Migration made no progress' + (': ' + str(issues[:8]) if issues else ''))
    return result, coverage, list(labels.values()), issues


def _validate_experience(before, after, records):
    units = {u.id: u for u in parse_experience_units(before['SKILL.md'])}
    expected, seen = {}, set()
    for record in sorted(records, key=lambda r: r['start']):
        unit = units.get(record['unit_id'])
        if (record['source'] != 'SKILL.md' or unit is None or unit.protected or unit.id in seen
                or (unit.start, unit.end, unit.content_sha256, unit.fingerprint)
                != (record['start'], record['end'], record['source_sha256'], record['fingerprint'])):
            raise UnsafeMarkdown('Coverage source span/hash mismatch')
        seen.add(unit.id)
        target = record['target']
        if REFERENCE_PATH.fullmatch(target) is None or not applies_when_valid(record['applies_when']):
            raise UnsafeMarkdown('Coverage target mismatch')
        block = moved_block(unit, target, before)
        old = expected.get(target, before.get(target, ''))
        if record['header']:
            if old or record['header'] != header(record['title'], record['applies_when']):
                raise UnsafeMarkdown('Coverage header mismatch')
            joined = record['header'] + '\n' + block
        elif not old:
            raise UnsafeMarkdown('Coverage header mismatch')
        else:
            joined = old + '\n' + block
        if (after.get(target, '')[record['target_start']:record['target_end']] != block
                or record['target_end'] != len(joined) or sha(block) != record['target_sha256']):
            raise UnsafeMarkdown('Coverage target mismatch')
        expected[target] = joined
        if record['navigation'] != navigation(unit, record['applies_when'], record['title'], target):
            raise UnsafeMarkdown('Coverage navigation mismatch')
    root = before['SKILL.md']
    for record in sorted(records, key=lambda r: r['start'], reverse=True):
        root = root[:record['start']] + record['navigation'] + root[record['end']:]
    if after['SKILL.md'] != canonical(root):
        raise UnsafeMarkdown('Unaccounted source change or lost protected core')
    for target, text in expected.items():
        if after.get(target) != text:
            raise UnsafeMarkdown('Unaccounted target change')
    touched = {'SKILL.md'} | set(expected)
    if any(before.get(p) != after.get(p) for p in before.keys() | after.keys() if p not in touched):
        raise UnsafeMarkdown('Unaccounted directory change')


def validate_coverage(before, after, records):
    """Reconstruct every source and target from exact spans; no semantic rewriting."""
    kinds = {record.get('kind', 'section') for record in records}
    if kinds == {'experience'}:
        return _validate_experience(before, after, records)
    if kinds - {'section'}:
        raise UnsafeMarkdown('Mixed or unknown coverage kinds')
    by_source, expected_targets = {}, {}
    for record in records:
        source, target = record['source'], record['target']
        units = {u.id: u for u in parse_units(before[source])}
        unit = units.get(record['unit_id'])
        if unit is None or (unit.start, unit.end, unit.content_sha256) != (record['start'], record['end'], record['source_sha256']):
            raise UnsafeMarkdown('Coverage source span/hash mismatch')
        if any(u.id == unit.id for u, _ in by_source.get(source, [])):
            raise UnsafeMarkdown('Duplicate coverage unit')
        if record['context_sha256'] != sha(unit.context):
            raise UnsafeMarkdown('Coverage dependency mismatch')
        block = canonical(rebase_links(unit.context + unit.text, source, target, before))
        if after[target][record['target_start']:record['target_end']] != block or sha(block) != record['target_sha256']:
            raise UnsafeMarkdown('Coverage target mismatch')
        by_source.setdefault(source, []).append((unit, target))
        old = expected_targets.get(target, before.get(target, ''))
        expected_targets[target] = old + ('\n' if old else '') + block
    for source, pairs in by_source.items():
        reconstructed = before[source]
        for unit, target in sorted(pairs, key=lambda pair: pair[0].start, reverse=True):
            reconstructed = reconstructed[:unit.start] + stub(unit, source, target) + reconstructed[unit.end:]
        if after[source] != canonical(reconstructed):
            raise UnsafeMarkdown('Unaccounted source change or lost protected core')
    for target, text in expected_targets.items():
        if after[target] != text:
            raise UnsafeMarkdown('Unaccounted target change')
    touched = set(by_source) | set(expected_targets)
    if any(before.get(p) != after.get(p) for p in before.keys() | after.keys() if p not in touched):
        raise UnsafeMarkdown('Unaccounted directory change')


def split_references(files, limit, *, sources=None):
    """Split oversized references only at independent leaf sections; keep navigation."""
    result, all_records = dict(files), []
    for source in sorted(p for p in files if REFERENCE_PATH.fullmatch(p) and (p in sources if sources is not None else len(files[p]) > limit)):
        before = dict(result)
        units = parse_units(before[source])
        records = []
        for unit in units:
            if unit.protected or not unit.applicability:
                continue
            target = 'references/split-' + sha(source + unit.id)[:24] + '.md'
            if target in result:
                raise UnsafeMarkdown('Reference split destination already exists')
            block = canonical(rebase_links(unit.context + unit.text, source, target, before))
            if len(block) > limit:
                raise UnsafeMarkdown('Reference has an indivisible oversized topic')
            result[target] = block
            records.append({'source': source, 'unit_id': unit.id, 'start': unit.start, 'end': unit.end,
                            'source_sha256': unit.content_sha256, 'context_sha256': sha(unit.context),
                            'target': target, 'target_start': 0, 'target_end': len(block),
                            'target_sha256': sha(block), 'method': 'verbatim_with_rebased_links'})
        if not records:
            raise UnsafeMarkdown('Oversized reference has no safely separable scoped topics')
        text = before[source]
        by_id = {u.id: u for u in units}
        for record in sorted(records, key=lambda r: r['start'], reverse=True):
            unit = by_id[record['unit_id']]
            text = text[:unit.start] + stub(unit, source, record['target']) + text[unit.end:]
        result[source] = canonical(text)
        validate_coverage(before, result, records)
        if len(result[source]) > limit:
            raise UnsafeMarkdown('Reference protected context remains oversized')
        all_records.extend(records)
    return result, all_records
