"""Atomic task commits. All other progress files are rebuildable projections."""
import os
import hashlib
import re
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from rsi.core.common import digest, read_json, write_json, safe_id
from rsi.experiment.run_logs import update_run
from rsi.experiment.tracking import summary_metrics, task_complete
from rsi.skills.store import load_initial_skill
from rsi.skills.directory import directory_schema


@contextmanager
def run_lock(output):
    """OS lock survives neither a normal exit nor a killed process."""
    with (Path(output) / 'run.lock').open('a+b') as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError('Run is already active; cannot resume concurrently') from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def initialize(output, tasks, config, version):
    schema = directory_schema(config)
    state = {'schema': 6 if schema else 5, 'tasks_hash': digest(tasks), 'config_hash': digest(config),
             'initial_skill_version': version, 'current_skill_version': version, 'completed': []}
    if schema:
        state['directory_schema'] = schema
    from rsi.skills.consolidation import settings as reference_settings
    if reference_settings(config)['enabled']:
        from rsi.skills.consolidation_state import initial_state
        state['reference_maintenance'] = initial_state()
    write_json(Path(output) / 'checkpoint.json', state)
    return state


def resume_request(output):
    output = Path(output).resolve()
    if not (output / 'checkpoint.json').exists():
        raise ValueError('This run has no task checkpoint; legacy runs cannot be resumed')
    tasks = read_json(output / 'task-manifest.json')
    config = read_json(output / 'config.json')
    state = read_json(output / 'checkpoint.json')
    schema = directory_schema(config)
    if (state.get('schema') != (6 if schema else 5) or (schema and state.get('directory_schema') != schema) or state.get('tasks_hash') != digest(tasks)
            or state.get('config_hash') != digest(config)):
        raise ValueError('Checkpoint schema, task manifest or configuration mismatch')
    completed = state['completed']
    for task in tasks:
        safe_id(task['id'])
    if len(completed) > len(tasks):
        raise ValueError('Checkpoint contains too many completed tasks')
    version = state['initial_skill_version']
    def verify_version(value):
        if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{16}', value):
            raise ValueError('Invalid checkpoint Skill version')
        files, _ = load_initial_skill(output / 'skills/versions' / value, directory_schema=schema)
        from rsi.skills.maintenance import settings, validate_limits
        validate_limits(files, settings(config))
    verify_version(version)
    from rsi.skills.consolidation import settings as reference_settings
    from rsi.skills.consolidation_state import initial_state, verify_entry
    reference = reference_settings(config)
    reference_state = initial_state()
    for task, entry in zip(tasks, completed):
        if reference['enabled']:
            reference_state = verify_entry(output, reference_state, entry['report'], entry['update'], config)
        verify_maintenance_artifacts(output, entry['report'], entry['update'], config)
        from rsi.skills.routing import verify_receipt
        verify_receipt(output, entry['report'], config, task)
        if (entry['report']['task_id'] != task['id'] or not task_complete(entry['report'])
                or entry['report'].get('phase') != ('train' if config['evolve'] else 'test')
                or entry['update']['before_skill_version'] != version
                or entry['report']['skill_version'] != version
                or not entry['update']['accepted'] and entry['update']['after_skill_version'] != version):
            raise ValueError('Checkpoint task order, score or Skill transition is invalid')
        version = entry['update']['after_skill_version']
        verify_version(version)
    if reference['enabled'] and state.get('reference_maintenance') != reference_state:
        raise ValueError('Consolidation checkpoint counter mismatch')
    if version != state['current_skill_version']:
        raise ValueError('Checkpoint Skill pointer mismatch')
    return state, config, [tasks]


def verify_maintenance_artifacts(output, report, update, config):
    from rsi.skills.maintenance import settings
    from rsi.skills.directory import reject_links
    if not settings(config)['enabled']:
        return
    artifacts = update.get('maintenance_artifacts')
    if artifacts is None and not update.get('accepted') and 'maintenance' not in update:
        return  # No proposal/no maintenance; no synthetic artifacts on historical runs.
    names = {'input.json', 'normalized-input.json', 'output.json', 'coverage.json',
             'normalization.json', 'validation.json', 'usage.json'}
    prefix = safe_id(report['task_id']) + '/maintenance/'
    if not isinstance(artifacts, dict) or prefix + 'validation.json' not in artifacts:
        raise ValueError('Missing maintenance receipt')
    for path, checksum in artifacts.items():
        if path not in {prefix + name for name in names}:
            raise ValueError('Invalid maintenance artifact path')
        location = Path(output) / path
        reject_links(location)
        if hashlib.sha256(location.read_bytes()).hexdigest() != checksum:
            raise ValueError('Maintenance artifact hash mismatch')
    receipt = read_json(Path(output) / prefix / 'validation.json')
    if receipt != update.get('maintenance'):
        raise ValueError('Maintenance receipt mismatch')
    if update.get('accepted') and (receipt.get('status') != 'accepted'
            or receipt.get('candidate_hash') != (update.get('learning_skill_version', update['after_skill_version'])
                if config.get('reference_maintenance', {}).get('enabled') else update['after_skill_version'])
            or prefix + 'output.json' not in artifacts):
        raise ValueError('Accepted Skill lacks a matching maintenance receipt')


def publish(output, state):
    """Idempotent replay after a crash during pointer/summary publication."""
    output = Path(output)
    if state.get('schema') == 6:
        if state.get('directory_schema') != 1:
            raise ValueError('Unsupported directory schema')
        for value in {state['initial_skill_version'], state['current_skill_version'],
                      *(entry['update']['after_skill_version'] for entry in state['completed'])}:
            if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{16}', value):
                raise ValueError('Invalid checkpoint Skill version')
            load_initial_skill(output / 'skills/versions' / value, directory_schema=1)
    reports = [entry['report'] for entry in state['completed']]
    history = []
    for entry in state['completed']:
        report, update = entry['report'], entry['update']
        folder = output / report['task_id']
        write_json(folder / 'summary.json', report)
        write_json(folder / 'update.json', update)
        history.append({'timestamp': entry['committed_at'], 'task_id': report['task_id'],
                        'before': update['before_skill_version'], 'after': update['after_skill_version'], **update})
    write_json(output / 'skills/current.json', {'version': state['current_skill_version']})
    import json
    path = output / 'skills/history.jsonl'
    temporary = path.with_suffix('.jsonl.tmp')
    temporary.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in history), encoding='utf-8')
    temporary.replace(path)
    write_json(output / 'summary.json', reports)
    write_json(output / 'metrics.json', summary_metrics(reports))
    update_run(output, completed_tasks=len(reports), scored_tasks=sum(r.get('phase') == 'test' for r in reports),
               supervised_tasks=sum(r.get('phase') == 'train' for r in reports),
               resolved=sum(bool(r['success']) for r in reports) if any(r.get('phase') == 'test' for r in reports) else None, current_skill_version=state['current_skill_version'])


def commit(output, state, report, update):
    if not task_complete(report):
        raise ValueError('Incomplete supervision or evaluation cannot be checkpointed')
    if state.get('schema') == 6:
        if state.get('directory_schema') != 1:
            raise ValueError('Unsupported directory schema')
        if (report['task_id'] in {e['report']['task_id'] for e in state['completed']}
                or report['skill_version'] != state['current_skill_version']
                or update['before_skill_version'] != state['current_skill_version']
                or not update['accepted'] and update['after_skill_version'] != state['current_skill_version']):
            raise ValueError('Invalid or duplicate managed task commit')
        value = update['after_skill_version']
        if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{16}', value):
            raise ValueError('Invalid Skill version')
        files, _ = load_initial_skill(Path(output) / 'skills/versions' / value, directory_schema=1)
        from rsi.skills.maintenance import settings, validate_limits
        validate_limits(files, settings(read_json(Path(output) / 'config.json')))
    if state.get('schema') == 6:
        config = read_json(Path(output) / 'config.json')
        verify_maintenance_artifacts(Path(output), report, update, config)
        from rsi.skills.routing import verify_receipt
        tasks = read_json(Path(output) / 'task-manifest.json')
        task = next((t for t in tasks if t['id'] == report['task_id']), None)
        verify_receipt(output, report, config, task)
    next_state = {**state, 'current_skill_version': update['after_skill_version'],
                  'completed': [*state['completed'], {'report': report, 'update': update,
                      'committed_at': datetime.now(timezone.utc).isoformat()}]}
    from rsi.skills.consolidation import settings as reference_settings
    if state.get('schema') == 6 and reference_settings(config)['enabled']:
        from rsi.skills.consolidation_state import verify_entry
        next_state['reference_maintenance'] = verify_entry(output, state['reference_maintenance'], report, update, config)
    # Single commit point: versions exist, but no pointer has changed yet.
    write_json(Path(output) / 'checkpoint.json', next_state)
    publish(output, next_state)
    return next_state


def archive_incomplete(output, tasks, completed):
    root = Path(output).resolve()
    for task in tasks[completed:]:
        folder = root / task['id']
        if not folder.exists():
            continue
        destination = root / 'attempts' / f"{task['id']}-{uuid.uuid4().hex[:12]}"
        if folder.is_symlink() or not folder.resolve().is_relative_to(root) or not destination.resolve().is_relative_to(root):
            raise ValueError('Attempt archive must remain within the run directory')
        destination.parent.mkdir(parents=True, exist_ok=True)
        folder.rename(destination)
