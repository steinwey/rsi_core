"""Summarize solver, grading and Trace2Skill artifacts without model calls."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from rsi.core.common import read_json, write_json


def main():
    launch = read_json(ROOT / '_work/active-parallel-launch.json')
    run = Path(launch['output'])
    tasks = read_json(run / 'task-manifest.json')
    report = {'run': str(run), 'monitor': read_json(Path(launch['monitor_status'])),
              'run_info': read_json(run / 'run-info.json'), 'tasks': [], 'json_responses': []}
    for task in tasks:
        folder = run / task['id']
        item = {'task_id': task['id'], 'routes': []}
        for index in range(5):
            path = folder / f'rollout-{index}/trajectory.json'
            if path.exists():
                route = read_json(path)
                item['routes'].append({key: route.get(key) for key in
                    ('rollout', 'status', 'error', 'skill_version', 'skill_injected')}
                    | {'steps': sum(m['role']=='assistant' for m in route['messages'])})
        engine = folder / 'trace2skill/summary.json'
        item['trace2skill'] = read_json(engine) if engine.exists() else None
        for name in ('selection', 'score', 'update', 'summary'):
            path = folder / f'{name}.json'
            item[name] = read_json(path) if path.exists() else None
        manifest = folder / 'evidence/manifest.json'
        if manifest.exists():
            item['evidence'] = read_json(manifest)
        report['tasks'].append(item)
    for path in run.rglob('*response.json'):
        data = read_json(path)
        content = data.get('content')
        if isinstance(content, str):
            try:
                value = json.loads(content)
                result = {'json_object': isinstance(value, dict)}
            except ValueError as exc:
                result = {'json_object': False, 'error': str(exc)}
            report['json_responses'].append({'file': str(path.relative_to(run)), **result})
    report['excluded_artifacts_found'] = [str(p.relative_to(run)) for pattern in ('*audit*', '*planner*')
                                         for p in run.rglob(pattern)]
    write_json(run / 'core-validation.json', report)
    print(json.dumps({'status': report['run_info']['status'], 'output': str(run / 'core-validation.json'),
                      'json_responses':len(report['json_responses']),
                      'non_object_responses':sum(not r['json_object'] for r in report['json_responses'])}, indent=2))


if __name__ == '__main__':
    main()
