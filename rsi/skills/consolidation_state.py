"""Checkpoint-owned schedule and receipts for optional reference maintenance."""
import hashlib
from pathlib import Path
from rsi.core.common import digest, read_json, safe_id
from rsi.skills.consolidation import settings, apply_plan
from rsi.skills.directory import reject_links
from rsi.skills.store import load_initial_skill


def initial_state():
    return {'accepted_updates': 0, 'checks': 0, 'cursor': 0}


def schedule(previous, config, effective):
    options = settings(config)
    after = dict(previous)
    if effective:
        after['accepted_updates'] += 1
    due = bool(effective and after['accepted_updates'] % options['interval_accepted_updates'] == 0)
    if due:
        after['checks'] += 1
        after['cursor'] += 1
    return {'before': previous, 'after': after, 'due': due, 'effective_learning_update': bool(effective)}


def verify_entry(output, previous, report, update, config):
    effective = (config['evolve'] and report.get('phase') == 'train' and update.get('accepted') is True
                 and update.get('method') == 'trace2skill'
                 and update.get('learning_skill_version') != update['before_skill_version'])
    expected = schedule(previous, config, effective)
    if update.get('reference_maintenance') != expected:
        raise ValueError('Consolidation schedule mismatch')
    if effective:
        version = update.get('learning_skill_version')
        if not isinstance(version, str) or len(version) != 16 or any(c not in '0123456789abcdef' for c in version):
            raise ValueError('Invalid learning savepoint version')
        seed, _ = load_initial_skill(Path(output) / 'skills/versions' / version, directory_schema=1)
        from rsi.skills.maintenance import validate_limits, settings as limits
        validate_limits(seed, limits(config))
    if expected['due']:
        prefix = safe_id(report['task_id']) + '/consolidation/'
        artifacts = update.get('consolidation_artifacts')
        required = {'savepoint.json', 'output.json', 'coverage.json', 'candidates.json', 'usage.json', 'validation.json'}
        if not isinstance(artifacts, dict) or not {prefix + p for p in required} <= artifacts.keys():
            raise ValueError('Missing consolidation receipts')
        for name, checksum in artifacts.items():
            tail = name.removeprefix(prefix)
            if not name.startswith(prefix) or '/' in tail or '\\' in tail or not tail.endswith('.json'):
                raise ValueError('Invalid consolidation artifact path')
            path = Path(output) / name; reject_links(path)
            if hashlib.sha256(path.read_bytes()).hexdigest() != checksum:
                raise ValueError('Consolidation artifact hash mismatch')
        folder = Path(output) / prefix
        receipt = read_json(folder / 'validation.json')
        saved = read_json(folder / 'savepoint.json')
        result = read_json(folder / 'output.json')
        if (receipt != update.get('consolidation') or saved != {'files': seed, 'version': digest(seed)}
                or receipt['savepoint_version'] != update['learning_skill_version']
                or receipt['output_version'] != update['after_skill_version']
                or digest(result['files']) != result['version'] or result['version'] != update['after_skill_version']):
            raise ValueError('Consolidation savepoint/output identity mismatch')
        if receipt['status'] == 'consolidated':
            if prefix + 'plan.json' not in artifacts:
                raise ValueError('Consolidated result lacks plan receipt')
            actual, ledger = apply_plan(seed, read_json(folder / 'plan.json'), read_json(folder / 'candidates.json')['files'], settings(config))
            coverage = read_json(folder / 'coverage.json')
            review = coverage.get('review', {}) if isinstance(coverage, dict) else {}
            if (actual != result['files'] or coverage != ledger | {'review': review}
                    or set(review) != {'approved', 'conflicts', 'uncertainties', 'reason'}
                    or review['approved'] is not True or review['conflicts'] != [] or review['uncertainties'] != []
                    or not isinstance(review['reason'], str) or not review['reason'].strip()):
                raise ValueError('Consolidation replay/coverage/review mismatch')
        elif receipt['status'] not in ('consolidation_failed', 'consolidation_skipped') or result['files'] != seed:
            raise ValueError('Failed consolidation did not retain savepoint')
    elif 'consolidation' in update or (effective and update['after_skill_version'] != update['learning_skill_version']):
        raise ValueError('Unexpected consolidation outside schedule')
    return expected['after']
