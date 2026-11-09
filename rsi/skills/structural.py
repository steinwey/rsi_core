"""Structural Migration trigger: a model-free light check plus a checkpoint-owned schedule.

Root length is deliberately not a signal. The light check only decides whether
the shared semantic migration should ask the model; it never decides what moves.
"""
import re
from rsi.core.common import digest
from rsi.skills.index import validate_index
from rsi.skills.markdown_units import parse_experience_units
from rsi.skills.routing import retrieve_bm25

MAX_PENDING, MAX_REVIEWED = 64, 512
IDENTIFIER = re.compile(
    r'`([^`\n]{2,80})`'
    r'|\b([A-Za-z_][\w]*(?:\.[A-Za-z_]\w*)+)\b'
    r'|\b([a-z][a-z0-9]*_[a-z0-9_]+)\b'
    r'|\b([a-z]+[A-Z][A-Za-z0-9]*|[A-Z][a-z0-9]+[A-Z][A-Za-z0-9]*)\b'
    r'|\b([A-Za-z_]\w*)\('
    r'|\b(\w+(?:Error|Exception|Warning))\b')
FINGERPRINT = re.compile(r'[0-9a-f]{64}')


def options(config):
    """Active only when a saved config explicitly opts in; legacy P1 runs stay unchanged."""
    from rsi.skills.maintenance import settings
    block = config.get('skill_maintenance', {})
    value = settings(config)
    if not value['enabled'] or block.get('structural_migration') is not True:
        return None
    return {'interval': value['structural_review_interval']}


def identifiers(text):
    found = []
    for match in IDENTIFIER.finditer(text):
        value = next(g for g in match.groups() if g).strip().lower()
        if len(value) >= 4:
            found.append(value)
    return list(dict.fromkeys(found))


def light_check(base, files, state):
    """Score changed root units by code-shaped identifiers, clustering and index overlap."""
    before = {u.fingerprint for u in parse_experience_units(base['SKILL.md'])}
    units = [u for u in parse_experience_units(files['SKILL.md']) if not u.protected]
    present = {u.fingerprint for u in units}
    reviewed = set(state['reviewed'])
    index = validate_index(files)
    vocab = {u.fingerprint: set(identifiers(u.text)) for u in units}
    signals = []
    for unit in units:
        if unit.fingerprint in before or unit.fingerprint in reviewed:
            continue
        ids = vocab[unit.fingerprint]
        cluster = any(ids & other for fp, other in vocab.items() if fp != unit.fingerprint)
        existing = bool(ids and index['entries']) and any(
            c.score > 0 for c in retrieve_bm25(' '.join(sorted(ids)), files, index))
        score = min(len(ids), 3) + int(cluster) + int(existing)
        signals.append({'fingerprint': unit.fingerprint, 'identifiers': sorted(ids)[:8],
                        'cluster': cluster, 'existing_topic': existing, 'score': score})
    candidates = sorted(s['fingerprint'] for s in signals if s['score'] >= 2)
    pending = sorted(set(state['pending']) & present)
    return {'changed_units': len(signals), 'signals': signals[:64], 'candidates': candidates,
            'pending': pending, 'triggered': bool(candidates or pending)}


def initial_state():
    return {'effective_updates': 0, 'full_reviews': 0, 'light_reviews': 0, 'pending': [], 'reviewed': []}


def request(state, opts):
    return {'due_full': (state['effective_updates'] + 1) % opts['interval'] == 0,
            'pending': list(state['pending']), 'reviewed': list(state['reviewed']),
            'effective_updates': state['effective_updates']}


def request_sha256(value):
    return digest({'structural_request': value})


def _fingerprints(value, limit):
    if (not isinstance(value, list) or len(value) > limit or value != sorted(set(value))
            or any(not isinstance(v, str) or FINGERPRINT.fullmatch(v) is None for v in value)):
        raise ValueError('Invalid structural migration fingerprints')
    return value


def advance(previous, receipt, effective, opts):
    after = dict(previous)
    if effective:
        if not isinstance(receipt, dict) or receipt.get('request_sha256') != request_sha256(request(previous, opts)):
            raise ValueError('Structural migration receipt does not match its request')
        after['effective_updates'] += 1
        after['full_reviews'] += receipt['mode'] in ('full', 'mandatory')
        after['light_reviews'] += receipt['mode'] == 'light'
        after['pending'] = _fingerprints(receipt['pending_after'], MAX_PENDING)
        after['reviewed'] = _fingerprints(receipt['reviewed_after'], MAX_REVIEWED)
    return {'before': previous, 'after': after, 'effective': bool(effective),
            'due_full': request(previous, opts)['due_full'],
            'mode': receipt.get('mode') if effective else None}


def effective_update(report, update, config):
    return bool(config['evolve'] and report.get('phase') == 'train' and update.get('accepted') is True
                and update.get('method', 'trace2skill') == 'trace2skill'
                and update.get('learning_skill_version', update['after_skill_version']) != update['before_skill_version'])


def verify_entry(previous, report, update, config):
    opts = options(config)
    effective = effective_update(report, update, config)
    receipt = (update.get('maintenance') or {}).get('structural')
    expected = advance(previous, receipt, effective, opts)
    if update.get('structural_migration') != expected:
        raise ValueError('Structural migration schedule mismatch')
    return expected['after']
