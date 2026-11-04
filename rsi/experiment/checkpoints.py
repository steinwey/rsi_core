"""Atomic task commits. All other progress files are rebuildable projections."""
import os
import re
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from rsi.core.common import digest, read_json, write_json, safe_id
from rsi.experiment.run_logs import update_run
from rsi.experiment.tracking import summary_metrics, task_complete
from rsi.skills.store import load_initial_skill


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
    state = {'schema': 5, 'tasks_hash': digest(tasks), 'config_hash': digest(config),
             'initial_skill_version': version, 'current_skill_version': version, 'completed': []}
    write_json(Path(output) / 'checkpoint.json', state)
    return state


def resume_request(output):
    output = Path(output).resolve()
    if not (output / 'checkpoint.json').exists():
        raise ValueError('This run has no task checkpoint; legacy runs cannot be resumed')
    tasks = read_json(output / 'task-manifest.json')
    config = read_json(output / 'config.json')
    state = read_json(output / 'checkpoint.json')
    if (state.get('schema') != 5 or state.get('tasks_hash') != digest(tasks)
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
        load_initial_skill(output / 'skills/versions' / value)
    verify_version(version)
    for task, entry in zip(tasks, completed):
        if (entry['report']['task_id'] != task['id'] or not task_complete(entry['report'])
                or entry['report'].get('phase') != ('train' if config['evolve'] else 'test')
                or entry['update']['before_skill_version'] != version
                or entry['report']['skill_version'] != version
                or not entry['update']['accepted'] and entry['update']['after_skill_version'] != version):
            raise ValueError('Checkpoint task order, score or Skill transition is invalid')
        version = entry['update']['after_skill_version']
        verify_version(version)
    if version != state['current_skill_version']:
        raise ValueError('Checkpoint Skill pointer mismatch')
    return state, config, [tasks]


def publish(output, state):
    """Idempotent replay after a crash during pointer/summary publication."""
    output = Path(output)
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
    next_state = {**state, 'current_skill_version': update['after_skill_version'],
                  'completed': [*state['completed'], {'report': report, 'update': update,
                      'committed_at': datetime.now(timezone.utc).isoformat()}]}
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
