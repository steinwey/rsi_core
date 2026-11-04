"""Replay real archived trajectories through both learning frameworks with monitoring."""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rsi import ROOT
from rsi.core.common import digest, file_map, read_json, write_json
from rsi.core.config import load_config, load_env, require_credentials
from rsi.core.model import Model
from rsi.core.responses import full_evidence
from rsi.frameworks import FRAMEWORKS, get_framework
from rsi.skills.store import SkillStore, document_gate, load_initial_skill
from scripts.monitor_experiment import cleanup_containers


def now():
    return datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds')


def cleanup_replay_containers(output):
    """Use only this replay's recorded names/IDs and grading directories."""
    logs = '\n'.join(p.read_text(encoding='utf-8', errors='replace')
                     for p in output.glob('*.stdout.log'))
    return cleanup_containers(output, logs)


def use_luna_learning(config, source):
    solver = config['model']
    if solver['name'] != 'gpt-6-luna' or read_json(source / 'config.json')['model']['name'] != 'gpt-6-luna':
        raise ValueError('Luna learning requires a Luna profile and archived Luna trajectories')
    for role in ('skill_error_model', 'skill_success_model', 'skill_editor_model'):
        config[role] = {
            **solver,
            **{key: config[role][key] for key in ('temperature', 'max_tokens', 'timeout', 'num_retries')},
        }


def worker(args):
    source = args.source.resolve()
    task = next(t for t in read_json(source / 'task-manifest.json') if t['id'] == args.task_id)
    old = source / task['id']
    results = [read_json(old / f'rollout-{i}/trajectory.json') for i in range(5)]
    versions = {r['skill_version'] for r in results}
    if len(versions) != 1 or any(r.get('error') for r in results):
        raise ValueError('Replay requires five completed routes using the same initial Skill')
    skill = source / 'skills/versions' / versions.pop()
    seed, metadata = load_initial_skill(skill)
    original = digest(file_map(skill))
    supervision = read_json(old / 'supervision.json')
    output = args.output / args.worker
    folder = output / task['id']
    folder.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    try:
        load_env()
        config = load_config(args.config, framework=args.worker)
        if args.luna_learning:
            use_luna_learning(config, source)
        require_credentials(config)
        write_json(output / 'config.json', config)
        write_json(output / 'task-manifest.json', [task])
        for i in range(5):
            shutil.copytree(old / f'rollout-{i}', folder / f'rollout-{i}',
                            ignore=shutil.ignore_patterns('logs'))
        write_json(folder / 'supervision.json', supervision)
        write_json(output / 'provenance.json', {
            'source_run': str(source), 'task_id': task['id'], 'source_skill': str(skill),
            'skill_hash': original, 'source_skill_metadata': metadata,
            'source_models': sorted({read_json(source / 'config.json')['model']['name']}),
            'source_labels': [r['success'] for r in supervision['routes']],
            'trajectory_sha256': {str(i): hashlib.sha256((old / f'rollout-{i}/trajectory.json').read_bytes()).hexdigest() for i in range(5)},
            'new_solver_calls': 0, 'learning_model': config['skill_editor_model']['name'],
            'note': 'Archived trajectories; Trace2Skill may re-evaluate diagnostic patches.'})
        framework = get_framework(config)
        framework.initialize(config)
        print(f'{now()} {args.worker}: learning from {task["id"]}', flush=True)
        proposal = framework.evolve_skill(task, results, skill, config, Model, folder=folder,
                                          supervision=supervision, prepared_evidence=full_evidence(results))
        store = SkillStore(output / 'skills', seed)
        candidate, diff = store.propose(proposal['changes'])
        changed = candidate != store.current()
        gate = document_gate(candidate, store.fixed_files, check_consistency=changed)
        if not gate['passed']:
            raise ValueError(f'Candidate document validation failed: {gate}')
        unchanged = digest(file_map(skill)) == original
        if not unchanged:
            raise RuntimeError('Source Skill changed during replay')
        store.record(candidate, changed, {'method': args.worker, 'task_id': task['id'], 'source_run': str(source),
                                         'validation_scope': gate['scope'], 'effectiveness_evaluated': False})
        (output / 'skill.diff').write_text(diff, encoding='utf-8')
        report = {'status': 'completed', 'method': args.worker, 'finished_at': now(),
                  'seconds': time.monotonic() - started, 'changed_files': list(proposal['changes']),
                  'candidate': str(candidate), 'document_gate': gate, 'source_skill_unchanged': unchanged,
                  'usage': read_json(folder / args.worker / 'usage.json')['total']}
        write_json(output / 'result.json', report)
        print(json.dumps(report, ensure_ascii=False), flush=True)
    except BaseException as exc:
        write_json(output / 'result.json', {'status': 'failed', 'method': args.worker, 'finished_at': now(),
                    'seconds': time.monotonic() - started, 'error': f'{type(exc).__name__}: {exc}'})
        raise


def monitor(args):
    args.output.mkdir(parents=True, exist_ok=False)
    load_env()
    require_credentials(load_config(args.config))
    docker = subprocess.run(['docker', 'info', '--format', '{{.ServerVersion}}'], capture_output=True, text=True, timeout=30)
    if docker.returncode:
        raise RuntimeError('Docker daemon unavailable: ' + docker.stderr)
    children, streams = {}, []
    started = time.monotonic()
    status_path = args.output / 'monitor.json'
    try:
        for method in FRAMEWORKS:
            out = (args.output / f'{method}.stdout.log').open('wb')
            err = (args.output / f'{method}.stderr.log').open('wb')
            streams.extend((out, err))
            command = [sys.executable, '-u', '-X', 'utf8', str(Path(__file__).resolve()),
                       '--source', str(args.source.resolve()), '--task-id', args.task_id,
                       '--config', str(args.config.resolve()), '--output', str(args.output.resolve()), '--worker', method]
            children[method] = subprocess.Popen(command, cwd=ROOT, stdout=out, stderr=err,
                                                env={**os.environ, 'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8'},
                                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        print(json.dumps({'output': str(args.output), 'monitor_pid': os.getpid(),
                          'worker_pids': {name: child.pid for name, child in children.items()}}, ensure_ascii=False), flush=True)
        while True:
            rows = {}
            for method, process in children.items():
                root = args.output / method
                files = list(root.rglob('*.json'))
                learning_files = list((root / args.task_id / method).rglob('*.json'))
                result_path = root / 'result.json'
                result = read_json(result_path) if result_path.exists() else None
                code = process.poll()
                paths = [*files, args.output / f'{method}.stdout.log', args.output / f'{method}.stderr.log']
                latest = max((p.stat().st_mtime for p in paths if p.exists()), default=time.time())
                rows[method] = {'pid': process.pid, 'running': code is None, 'exit_code': code,
                                'status': result['status'] if result else ('running' if code is None else 'failed'),
                                'idle_seconds': round(time.time() - latest),
                                'requests': sum(p.name.endswith('.request.json') for p in learning_files),
                                'responses': sum(p.name.endswith('.response.json') for p in learning_files),
                                'analysis_reports': len(list(root.rglob('analysis_report.md'))),
                                'call_errors': sum(p.name.endswith('.error.json') for p in learning_files), 'result': result}
            done = all(not r['running'] for r in rows.values())
            status = {'checked_at': now(), 'monitor_pid': os.getpid(), 'source': str(args.source.resolve()),
                      'task_id': args.task_id, 'seconds': round(time.monotonic() - started),
                      'status': ('completed' if all(r['status'] == 'completed' for r in rows.values()) else 'failed') if done else 'running',
                      'workers': rows}
            # Single writer; consumers get atomic status snapshots.
            temporary = status_path.with_suffix('.tmp')
            write_json(temporary, status)
            temporary.replace(status_path)
            if done:
                print(json.dumps(status, ensure_ascii=False), flush=True)
                return 0 if status['status'] == 'completed' else 1
            if any(not r['running'] and r['status'] != 'completed' for r in rows.values()):
                raise RuntimeError('Replay worker failed; stopping only this replay\'s remaining workers')
            if time.monotonic() - started > args.timeout:
                raise TimeoutError(f'Replay exceeded monitor wall-time limit {args.timeout}s')
            time.sleep(15)
    except BaseException as exc:
        for process in children.values():
            if process.poll() is None:
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True, timeout=30)
                process.wait(timeout=10)
        write_json(args.output / 'monitor-error.json', {'error': f'{type(exc).__name__}: {exc}', 'at': now()})
        write_json(status_path, {'status': 'failed', 'reason': f'{type(exc).__name__}: {exc}',
                                'checked_at': now(), 'monitor_pid': os.getpid()})
        raise
    finally:
        for stream in streams:
            stream.close()
        cleanup = cleanup_replay_containers(args.output)
        write_json(args.output / 'container-cleanup.json', cleanup)
        if any(not item.get('removed') for item in cleanup):
            status = read_json(status_path) or {}
            status.update(status='failed', reason='Replay owned-container cleanup failed', checked_at=now())
            write_json(status_path, status)
            raise RuntimeError('Replay owned-container cleanup failed; see container-cleanup.json')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--task-id', required=True)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--config', type=Path, default=ROOT / 'configs/agent/python-pilot.yaml')
    parser.add_argument('--worker', choices=FRAMEWORKS, help=argparse.SUPPRESS)
    parser.add_argument('--luna-learning', action='store_true', help='Replay one archived Luna group with Luna learning roles')
    parser.add_argument('--timeout', type=int, default=3600)
    args = parser.parse_args()
    if args.luna_learning and args.worker != 'trace2skill':
        parser.error('--luna-learning requires --worker trace2skill')
    if args.worker:
        worker(args)
    else:
        raise SystemExit(monitor(args))
