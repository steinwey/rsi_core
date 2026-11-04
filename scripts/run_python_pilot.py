"""Launch the prepared Python pilot, with an explicit frozen evaluation handoff."""
import argparse
import hashlib
import json
import subprocess
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rsi import ROOT
from rsi.core.common import read_json, write_json
from rsi.core.config import load_config, load_benchmark_config
from rsi.benchmarks.tasks import validate_swe_tasks
from rsi.skills.store import load_initial_skill


def check_manifests():
    split = read_json(ROOT / 'tasks/python-pilot-split.json')
    groups = {}
    for name in ('train', 'eval'):
        path = ROOT / f'tasks/python-pilot-{name}-10.json'
        if hashlib.sha256(path.read_bytes()).hexdigest() != split['manifest_sha256'][name]:
            raise ValueError(f'{name} manifest differs from the prepared split')
        groups[name] = read_json(path)
        if len(groups[name]) != 10 or any(t['benchmark'] != 'swe' for t in groups[name]):
            raise ValueError('Expected ten independent SWE tasks per split')
    tasks = groups['train'] + groups['eval']
    if len({t['id'] for t in tasks}) != 20:
        raise ValueError('Training and evaluation overlap')
    if len({t['source_instance_id'] for t in split['tasks']}) != 20:
        raise ValueError('Duplicate source milestones')
    if any(t['language'] != 'python' for t in split['tasks']):
        raise ValueError('Pilot must contain Python tasks only')
    dataset = Path(split['evaluation_dataset'])
    if hashlib.sha256(dataset.read_bytes()).hexdigest() != split['evaluation_sha256']:
        raise ValueError('Cached evaluation data changed')
    config = load_config(ROOT / 'configs/agent/python-pilot.yaml')
    config['swe_dataset'] = str(dataset)
    validate_swe_tasks(tasks, config)
    return groups, split


def evaluation_spec(training_run, groups, split, *, eval_tasks=None, label='python-pilot-eval-10'):
    training_run = Path(training_run).resolve()
    info = read_json(training_run / 'run-info.json')
    if (info.get('status') != 'completed' or info.get('mode') != 'evolve'
            or info.get('completed_tasks') != 10 or info.get('supervised_tasks') != 10):
        raise ValueError('Evaluation requires a completed, supervised ten-task training run in evolve mode')
    actual = read_json(training_run / 'task-manifest.json')
    expected = groups['train']
    if len(actual) != len(expected) or any(
            any(row.get(k) != value for k, value in task.items()) for row, task in zip(actual, expected)):
        raise ValueError('Training run does not match the prepared training tasks and order')
    config_path = training_run / 'config.json'
    config = load_config(config_path)
    if not config.get('evolve') or config.get('swe_dataset') != split['evaluation_dataset']:
        raise ValueError('Training configuration does not match the pilot dataset or evolve mode')
    version = read_json(training_run / 'skills/current.json')['version']
    if version != info.get('current_skill_version'):
        raise ValueError('Training Skill pointer differs from completed run metadata')
    skill_path = training_run / 'skills/versions' / version
    load_initial_skill(skill_path)  # Verify the version hash and immutable tools.
    return {'config': str(config_path), 'tasks': str(eval_tasks or ROOT / 'tasks/python-pilot-eval-10.json'),
            'rollouts': 5, 'parallel': True, 'fixed_skill': True, 'initial_skill': str(skill_path),
            'swe_dataset': split['evaluation_dataset'], 'swe_split': config['swe_split'],
            'label': label, 'wandb': False}


def require_local_images(tasks):
    images = sorted({task['instance']['image'] for task in tasks})
    result = subprocess.run(['docker', 'image', 'inspect', '--format', '{{.Id}}', *images],
                            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=90)
    if result.returncode:
        raise RuntimeError('Docker unavailable or required cached images missing. No experiment started.\n' + result.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('check', 'train', 'evaluate'))
    parser.add_argument('--training-run', type=Path)
    args = parser.parse_args()
    if (args.action == 'evaluate') != (args.training_run is not None):
        parser.error('--training-run is required only for evaluate')
    groups, split = check_manifests()
    if args.action == 'check':
        print(json.dumps({'train': 10, 'eval': 10, 'unique_sources': 20,
                          'python_only': True, 'metadata_valid': True, 'images_checked': False}, indent=2))
        return 0
    if args.action == 'train':
        path = ROOT / 'configs/benchmark/python-pilot-train.yaml'
        settings = load_benchmark_config(path)
        if Path(settings['tasks']) != ROOT / 'tasks/python-pilot-train-10.json' or settings.get('fixed_skill'):
            raise ValueError('Training benchmark must use the pilot training manifest with evolution enabled')
        tasks = groups['train']
    else:
        spec = evaluation_spec(args.training_run, groups, split)
        path = ROOT / '_work' / f'python-pilot-eval-{uuid.uuid4().hex[:12]}.yaml'
        write_json(path, spec)  # JSON is valid YAML; all paths are absolute.
        tasks = groups['eval']
    require_local_images(tasks)
    print(f'Launching {args.action}: {path}', flush=True)
    return subprocess.call([sys.executable, '-u', '-X', 'utf8', str(ROOT / 'scripts/monitor_experiment.py'),
                            '--benchmark-config', str(path)], cwd=ROOT)


if __name__ == '__main__':
    raise SystemExit(main())
