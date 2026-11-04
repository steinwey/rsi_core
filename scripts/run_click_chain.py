"""Click chronological 10-train/5-test pilot; monitored training then frozen evaluation."""
import argparse
import hashlib
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rsi import ROOT
from rsi.core.common import read_json, write_json
from rsi.core.config import load_config
from rsi.benchmarks.tasks import validate_swe_tasks
from scripts.run_python_pilot import evaluation_spec, require_local_images
from scripts.monitor_experiment import monitor


def check_manifests():
    split = read_json(ROOT / 'tasks/click-chain-split.json')
    groups = {}
    for name, count in (('train', 10), ('eval', 5)):
        path = ROOT / f'tasks/click-chain-{name}.json'
        if hashlib.sha256(path.read_bytes()).hexdigest() != split['manifest_sha256'][name]:
            raise ValueError('Click manifest hash mismatch')
        groups[name] = read_json(path)
        if len(groups[name]) != count or any(t['benchmark'] != 'swe' or t['instance']['repo'] != 'pallets/click' for t in groups[name]):
            raise ValueError('Expected Click-only 10/5 split')
    source = Path(split['source_manifest'])
    if hashlib.sha256(source.read_bytes()).hexdigest() != split['source_sha256']:
        raise ValueError('Cached source changed')
    rows = [t for t in read_json(source) if t.get('chain', {}).get('id') == split['chain_id']]
    if len(rows) != 15 or [t['chain']['index'] for t in rows] != list(range(15)):
        raise ValueError('Expected complete chronological 15-task source chain')
    tasks = groups['train'] + groups['eval']
    if len({t['id'] for t in tasks}) != 15:
        raise ValueError('Train/test overlap')
    for task, row in zip(tasks, rows):
        expected = {k: v for k, v in row.items() if k != 'chain'}
        expected['benchmark'] = 'swe'
        if task != expected:
            raise ValueError('Manifest must match original chronological source')
    dataset = Path(split['evaluation_dataset'])
    if hashlib.sha256(dataset.read_bytes()).hexdigest() != split['evaluation_sha256']:
        raise ValueError('Cached evaluation data changed')
    config = load_config(ROOT / 'configs/agent/python-pilot.yaml')
    config['swe_dataset'] = str(dataset)
    validate_swe_tasks(tasks, config)
    return groups, split


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('check', 'run', 'evaluate'))
    parser.add_argument('--training-run', type=Path)
    args = parser.parse_args()
    if (args.action == 'evaluate') != (args.training_run is not None):
        parser.error('--training-run is required only for evaluate')
    groups, split = check_manifests()
    if args.action == 'check':
        print(json.dumps({'repo': split['repo'], 'train': 10, 'test': 5, 'chronological': True, 'metadata_valid': True}))
        return 0
    require_local_images(groups['train'] + groups['eval'])
    workflow_path = ROOT / '_work' / f'click-chain-{uuid.uuid4().hex[:12]}.workflow.json'
    state = {'phase': 'train' if args.action == 'run' else 'test', 'status': 'running'}
    write_json(workflow_path, state)
    try:
        training = args.training_run
        if args.action == 'run':
            code = monitor(benchmark_config=ROOT / 'configs/benchmark/click-chain-train.yaml')
            launch = read_json(ROOT / '_work/active-parallel-launch.json')
            state['training_run'] = launch['output']
            if code:
                raise RuntimeError('Training monitor stopped; evaluation was not started')
            training = Path(launch['output'])
        state.update(training_run=str(training.resolve()), phase='test')
        spec = evaluation_spec(training, groups, split, eval_tasks=ROOT / 'tasks/click-chain-eval.json', label='click-chain-test-5')
        state['frozen_skill'] = spec['initial_skill']
        write_json(workflow_path, state)
        spec_path = workflow_path.with_suffix('.eval.json')
        write_json(spec_path, spec)
        code = monitor(benchmark_config=spec_path)
        state['test_run'] = read_json(ROOT / '_work/active-parallel-launch.json')['output']
        if code:
            raise RuntimeError('Evaluation monitor stopped; inspect saved logs')
        state.update(status='completed', phase='completed')
        write_json(workflow_path, state)
        return 0
    except BaseException as exc:
        state.update(status='stopped', error=f'{type(exc).__name__}: {exc}')
        write_json(workflow_path, state)
        raise


if __name__ == '__main__':
    raise SystemExit(main())
