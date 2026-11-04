"""Read live trajectory counters without changing the training process."""
import argparse
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rsi import ROOT
from rsi.core.common import read_json, write_json
from scripts.monitor_experiment import now


def json_lines(path):
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding='utf-8', errors='replace').splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass  # The writer may not yet have finished the final line.
    return rows


def trajectory_counters(task_folder, count, *, terminal=False):
    rows = []
    for index in range(count):
        folder = task_folder / f'rollout-{index}'
        path = folder / 'trajectory.json'
        commands = json_lines(folder / 'commands/index.jsonl')
        actions = json_lines(folder / 'action-protocol.jsonl')
        latest_call = max((a.get('model_call', 0) for a in actions), default=0)
        row = {'rollout': index, 'status': ('interrupted' if terminal else 'running') if folder.exists() else 'queued',
               'solver_calls': latest_call, 'solver_calls_are_final': False,
               'assistant_steps': None, 'executed_commands': len(commands),
               'format_repair_requests': len(list((folder / 'action-repair').glob('*.request.json'))),
               'format_failures': max((a.get('total_failures', 0) for a in actions), default=0),
               'trajectory_saved': path.exists()}
        if path.exists():
            result = read_json(path)
            row.update(status=result['status'], solver_calls=result['usage']['calls'], solver_calls_are_final=True,
                       assistant_steps=sum(m.get('role') == 'assistant' for m in result['messages']),
                       executed_commands=result.get('executed_commands', len(commands)),
                       repair_attempts=result.get('repair_attempts', 0), seconds=result.get('seconds'),
                       error=result.get('error'))
        rows.append(row)
    return rows


def snapshot(workflow):
    state = read_json(workflow / 'workflow.json')
    previous = workflow / 'trajectory-steps.json'
    if state['status'] != 'running' and previous.exists():
        data = read_json(previous)
        data.update(checked_at=now(), workflow_status=state['status'], terminal=True)
        for row in data['routes']:
            if row['status'] == 'running':
                row['status'] = 'interrupted'
        return data
    launch = read_json(ROOT / '_work/active-parallel-launch.json')
    run = Path(launch['output']).resolve()
    belongs = str(run) == state.get('training_run') or str(run) == state.get('test_run')
    if not belongs:
        # The evaluation run is assigned to workflow.test_run only after it exits.
        belongs = (state.get('phase') == 'test' and launch.get('benchmark_config')
                   and Path(launch['benchmark_config']).resolve() == workflow / 'eval-spec.json')
    if not belongs:
        raise ValueError('Active launch does not belong to this workflow')
    config = read_json(run / 'config.json')
    monitor = read_json(launch['monitor_status'])
    checkpoint = read_json(run / 'checkpoint.json')
    task_id = monitor.get('current_task')
    terminal = state['status'] != 'running' or monitor['status'] in ('stopped', 'stop_failed')
    return {'checked_at': now(), 'workflow_status': state['status'], 'monitor_status': monitor['status'],
            'phase': 'train' if config['evolve'] else 'test', 'run': str(run), 'task_id': task_id,
            'completed_tasks': len(checkpoint['completed']), 'step_limit': config['step_limit'],
            'total_routes': config['rollouts'], 'max_concurrent': min(config['rollouts'], config.get('rollout_workers', config['rollouts'])),
            'note': 'Unfinished solver-call counts are lower bounds from action logs; API calls still in flight may not be recorded yet.',
            'routes': trajectory_counters(run / task_id, config['rollouts'], terminal=terminal) if task_id else [],
            'terminal': terminal}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workflow-dir', required=True, type=Path)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--poll-seconds', type=int, default=30)
    args = parser.parse_args()
    if args.poll_seconds < 1:
        parser.error('--poll-seconds must be positive')
    workflow = args.workflow_dir.resolve()
    write_json(workflow / 'trajectory-step-watcher.json', {'pid': os.getpid(), 'status': 'running', 'started_at': now()})
    previous = None
    try:
        while True:
            try:
                data = snapshot(workflow)
            except FileNotFoundError:
                if args.once:
                    raise
                time.sleep(args.poll_seconds)  # A new phase may be launched before its config/checkpoint is written.
                continue
            write_json(workflow / 'trajectory-steps.json', data)
            signature = json.dumps({k: v for k, v in data.items() if k != 'checked_at'}, sort_keys=True)
            if signature != previous:
                with (workflow / 'trajectory-steps.jsonl').open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps(data, ensure_ascii=False) + '\n')
                previous = signature
            if args.once or data['terminal']:
                break
            time.sleep(args.poll_seconds)
        write_json(workflow / 'trajectory-step-watcher.json', {'pid': os.getpid(), 'status': 'completed', 'finished_at': now()})
    except BaseException as exc:
        write_json(workflow / 'trajectory-step-watcher.json', {'pid': os.getpid(), 'status': 'failed', 'error': str(exc)})
        raise


if __name__ == '__main__':
    main()
