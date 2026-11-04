"""Bounded MigrationPlan validation and deterministic, lossless application."""
import re
from rsi.core.common import digest
from rsi.skills.directory import REFERENCE_PATH, validate_paths
from rsi.skills.markdown_units import parse_units, rebase_links, stub, sha, canonical, UnsafeMarkdown

PLAN_FIELDS = {'schema', 'input_hash', 'operations'}
OP_FIELDS = {'unit_id', 'source_sha256', 'start', 'end', 'target', 'operation',
             'target_content', 'classification', 'applies_when', 'coverage', 'reason'}


def apply_plan(files, plan, max_reference_chars):
    if (not isinstance(plan, dict) or set(plan) != PLAN_FIELDS or type(plan['schema']) is not int
            or plan['schema'] != 1 or plan['input_hash'] != digest(files)):
        raise UnsafeMarkdown('Invalid MigrationPlan identity')
    operations = plan['operations']
    if not isinstance(operations, list) or not 1 <= len(operations) <= 128:
        raise UnsafeMarkdown('MigrationPlan requires bounded nonempty operations')
    units = {u.id: u for u in parse_units(files['SKILL.md'])}
    result, selected, coverage = dict(files), {}, []
    for op in operations:
        if not isinstance(op, dict) or set(op) != OP_FIELDS:
            raise UnsafeMarkdown('Invalid MigrationPlan operation fields')
        unit = units.get(op['unit_id']) if isinstance(op['unit_id'], str) else None
        if unit is None or unit.id in selected or unit.protected or not unit.applicability:
            raise UnsafeMarkdown('Unknown, duplicate, protected or unscoped source unit')
        if (op['classification'] != 'specific' or op['operation'] != 'move_verbatim'
                or type(op['start']) is not int or type(op['end']) is not int
                or (op['start'], op['end'], op['source_sha256']) != (unit.start, unit.end, unit.content_sha256)
                or op['target_content'] != unit.text or op['coverage'] != [unit.id]
                or op['applies_when'] not in unit.applicability
                or not isinstance(op['reason'], str) or not op['reason'].strip() or len(op['reason']) > 2000):
            raise UnsafeMarkdown('Uncertain classification, rewriting or incomplete source coverage')
        target = op['target']
        if not isinstance(target, str) or REFERENCE_PATH.fullmatch(target) is None:
            raise UnsafeMarkdown('Illegal migration target')
        validate_paths([*result, *([] if target in result else [target])])
        # Context is deterministic, not model-supplied. Copies preserve ancestor constraints.
        block = canonical(rebase_links(unit.context + unit.text, 'SKILL.md', target, files))
        previous = result.get(target, '')
        joined = previous + ('\n' if previous else '') + block
        if len(joined) > max_reference_chars:
            # Do not split an atomic section or overfill an existing related reference.
            # Give this independent topic its own deterministic sibling file.
            target = 'references/moved-' + unit.id + '.md'
            if target in result:
                raise UnsafeMarkdown('Migration split destination already exists')
            block = canonical(rebase_links(unit.context + unit.text, 'SKILL.md', target, files))
            joined, previous = block, ''
        if len(joined) > max_reference_chars:
            raise UnsafeMarkdown('Atomic section plus dependencies exceeds reference limit')
        result[target] = joined
        selected[unit.id] = (unit, target)
        coverage.append({'source': 'SKILL.md', 'unit_id': unit.id, 'start': unit.start, 'end': unit.end,
                         'source_sha256': unit.content_sha256, 'context_sha256': sha(unit.context),
                         'target': target, 'target_start': len(previous) + (1 if previous else 0),
                         'target_end': len(joined), 'target_sha256': sha(block),
                         'applies_when': op['applies_when'], 'method': 'verbatim_with_rebased_links'})
    root = files['SKILL.md']
    for unit, target in sorted(selected.values(), key=lambda pair: pair[0].start, reverse=True):
        root = root[:unit.start] + stub(unit, 'SKILL.md', target) + root[unit.end:]
    result['SKILL.md'] = canonical(root)
    validate_coverage(files, result, coverage)
    if len(result['SKILL.md']) >= len(files['SKILL.md']):
        raise UnsafeMarkdown('Migration made no progress')
    return result, coverage


def validate_coverage(before, after, records):
    """Reconstruct every source and target from exact spans; no semantic rewriting."""
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


def split_references(files, limit):
    """Split oversized references only at independent leaf sections; keep navigation."""
    result, all_records = dict(files), []
    for source in sorted(p for p in files if REFERENCE_PATH.fullmatch(p) and len(files[p]) > limit):
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
