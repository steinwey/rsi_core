"""Train Trace2Skill on all 15 Click milestones, then replay with a frozen Skill."""
import argparse
import hashlib
import json
import shutil
import uuid
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rsi import ROOT
from rsi.core.common import read_json, write_json, digest
from rsi.core.config import load_config, load_env, require_credentials, model_configs
from rsi.benchmarks.tasks import validate_swe_tasks
from rsi.skills.store import load_initial_skill
from scripts.run_click_chain import check_manifests
from scripts.run_python_pilot import require_local_images
from scripts.monitor_experiment import monitor, now
from rsi.experiment.checkpoints import run_lock, resume_request, publish
from rsi.experiment.run_logs import create_run_directory, update_run

MANIFEST = ROOT / 'tasks/click-chain-15.json'
TRAIN_SPEC = ROOT / 'configs/benchmark/click-chain-15-train.yaml'
CONFIG = ROOT / 'configs/agent/click15-flash-max.yaml'


def preflight(config_path=CONFIG):
    groups, split = check_manifests()
    tasks = read_json(MANIFEST)
    expected = groups['train'] + groups['eval']
    if len(tasks) != 15 or [t.get('chain', {}).get('index') for t in tasks] != list(range(15)):
        raise ValueError('Require the complete chronological Click 15-task chain')
    if [{k: v for k, v in t.items() if k != 'chain'} for t in tasks] != expected:
        raise ValueError('Full-chain manifest differs from the verified original milestones')
    config = load_config(config_path)
    if config['skill_update_method'] != 'trace2skill' or config['model']['name'] not in ('qwen3.8-flash', 'gpt-6-luna'):
        raise ValueError('This experiment requires Flash or Luna solving with Trace2Skill')
    validate_swe_tasks(tasks, config)
    load_env()
    require_credentials(config)
    require_local_images(tasks)
    return tasks, split, config


def freeze_training(training, tasks, workflow):
    state = read_json(training / 'checkpoint.json')
    info = read_json(training / 'run-info.json')
    actual = read_json(training / 'task-manifest.json')
    if (info.get('status') != 'completed' or info.get('mode') != 'evolve'
            or len(state['completed']) != 15 or info.get('supervised_tasks') != 15
            or [t['id'] for t in actual] != [t['id'] for t in tasks]):
        raise ValueError('All fifteen chronological training tasks must complete before evaluation')
    for entry in state['completed']:
        if entry['report'].get('phase') != 'train' or not entry['report'].get('supervision_complete'):
            raise ValueError('Training supervision incomplete')
        if entry['update']['decision'] in ('checks_failed', 'update_error'):
            raise ValueError('Training has a rejected or failed Skill update')
    config = load_config(training / 'config.json')
    solver = model_configs(config)['model']['name']
    if solver not in ('qwen3.8-flash', 'gpt-6-luna'):
        raise ValueError('Training solver model mismatch')
    version = state['current_skill_version']
    if read_json(training / 'skills/current.json')['version'] != version:
        raise ValueError('Checkpoint and current Skill pointer differ')
    skill = training / 'skills/versions' / version
    load_initial_skill(skill)
    config['evolve'] = False
    config.update(execution_mode='single', rollouts=1, rollout_workers=1)
    config['model']['temperature'] = 0.0
    config['initial_skill'] = str(skill)
    expected = {solver}
    if config.get('action_format_repair_enabled'):
        expected.add(config['judge_model']['name'])
    if {block['name'] for block in model_configs(config, active_only=True).values()} != expected:
        raise ValueError('Frozen evaluation must retain its solver and configured format-repair model')
    eval_config = workflow / 'eval-config.json'
    write_json(eval_config, config)
    load_config(eval_config)
    spec = {'config': str(eval_config), 'tasks': str(MANIFEST), 'rollouts': 1, 'parallel': False,
            'fixed_skill': True, 'initial_skill': str(skill), 'swe_dataset': config['swe_dataset'],
            'swe_split': config['swe_split'], 'label': f'click15-frozen-{solver}-retest', 'wandb': False}
    path = workflow / 'eval-spec.json'
    write_json(path, spec)
    return path, skill


def fork_training_checkpoint(source, tasks, workers, *, step_limit=None, rollout_timeout=None,
                             rate_limit_retry_delay=None, runs_dir=None):
    """Preserve committed work while auditing explicit execution-limit changes."""
    source = Path(source).resolve()
    with run_lock(source):
        state, previous, groups = resume_request(source)
        actual = groups[0]
        if (not previous.get('evolve') or previous.get('skill_update_method') != 'trace2skill'
                or len(tasks) != 15 or not 1 <= len(actual) <= 15 or len(state['completed']) >= 15
                or any(any(row.get(k) != v for k, v in expected.items()) for row, expected in zip(actual, tasks))):
            raise ValueError('Resume requires the matching incomplete Click15 Trace2Skill training checkpoint')
        source_task_count = len(actual)
        # A bounded task-2 trial may have committed a prefix of the full chain.
        # Append only the verified remaining tasks and retain normalized SWE prompts.
        actual = [*actual, *[{**task, 'prompt': task['instance']['problem_statement']}
                            for task in tasks[source_task_count:]]]
        if type(workers) is not int or not 1 <= workers <= 5:
            raise ValueError('Resume worker count must be between one and five')
        overrides = {'rollout_workers': workers}
        if step_limit is not None:
            if type(step_limit) is not int or step_limit < 0:
                raise ValueError('Resume step limit must be a nonnegative integer')
            overrides['step_limit'] = step_limit
        if rollout_timeout is not None:
            if type(rollout_timeout) not in (int, float) or rollout_timeout <= 0:
                raise ValueError('Resume rollout timeout must be positive')
            overrides['rollout_timeout'] = rollout_timeout
        config = {**previous, **overrides}
        changes = {key: {'before': previous.get(key, previous['rollouts'] if key == 'rollout_workers' else None),
                         'after': value} for key, value in overrides.items()}
        if rate_limit_retry_delay is not None:
            if (type(rate_limit_retry_delay) not in (int, float)
                    or not 0 < rate_limit_retry_delay < float('inf')):
                raise ValueError('Resume rate-limit retry delay must be positive and finite')
            for role in model_configs(previous):
                config[role] = {**previous[role], 'rate_limit_retry_delay': rate_limit_retry_delay}
                changes[f'{role}.rate_limit_retry_delay'] = {
                    'before': previous[role].get('rate_limit_retry_delay'), 'after': rate_limit_retry_delay}
        seed_source = read_json(source / 'initial-skill-source.json')
        # Diagnostic harness logs already have deeply nested paths on Windows.
        target_root = Path(runs_dir or ROOT / 'runs')
        target = create_run_directory(actual, config,
            output=target_root / f'click15-resume-{uuid.uuid4().hex[:8]}',
            label=f'click15-resume-{workers}workers', initial_skill=seed_source)
        write_json(target / 'config.json', config)
        write_json(target / 'task-manifest.json', actual)
        write_json(target / 'initial-skill-source.json', seed_source)
        for filename in ('upstreams.lock.json', 'fixed-tools-check.json'):
            if (source / filename).exists():
                shutil.copy2(source / filename, target / filename)
        for directory in ('initial-skill', 'skills'):
            shutil.copytree(source / directory, target / directory)
        for entry in state['completed']:
            task_id = entry['report']['task_id']
            shutil.copytree(source / task_id, target / task_id)
        forked = {**state, 'config_hash': digest(config), 'tasks_hash': digest(actual)}
        write_json(target / 'checkpoint.json', forked)
        write_json(target / 'resume-source.json', {
            'source_run': str(source), 'source_config_hash': state['config_hash'],
            'source_checkpoint_sha256': hashlib.sha256((source / 'checkpoint.json').read_bytes()).hexdigest(),
            'source_tasks_hash': state['tasks_hash'],
            'task_manifest_extension': {'before': source_task_count, 'after': len(actual),
                                        'added_task_ids': [task['id'] for task in actual[source_task_count:]]},
            'preserved_completed_tasks': len(state['completed']),
            'current_skill_version': state['current_skill_version'],
            'config_changes': changes,
            'uncommitted_attempts_preserved_in_source': [t['id'] for t in actual[len(state['completed']):] if (source / t['id']).exists()]})
        publish(target, forked)
        update_run(target, status='interrupted', resumed_from=str(source), rollout_workers=workers)
        resume_request(target)
        return target, len(state['completed'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workflow-dir', type=Path, required=True)
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--resume-training', type=Path)
    parser.add_argument('--config', type=Path, default=CONFIG)
    args = parser.parse_args()
    tasks, split, config = preflight(args.config)
    evaluation_models = {'model': config['model']['name'], 'judge_model': config['judge_model']['name']}
    report = {'training_tasks': 15, 'evaluation_tasks': 15, 'same_tasks_retested': True,
              'task_order': [t['id'] for t in tasks], 'framework': 'trace2skill',
              'training_models': {role: block['name'] for role, block in model_configs(config, active_only=True).items()},
              'evaluation_active_models': evaluation_models,
              'rollouts': 5, 'parallel': True, 'step_limit': config['step_limit'],
              'rollout_workers': config.get('rollout_workers', 5),
              'evaluation_rollouts': 1, 'evaluation_selector': False,
              'evaluation_solver_temperature': 0.0,
              'rollout_timeout': config['rollout_timeout'], 'trace2skill': config['trace2skill'],
              'manifest_sha256': hashlib.sha256(MANIFEST.read_bytes()).hexdigest(),
              'evaluation_dataset_sha256': split['evaluation_sha256']}
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    if args.check:
        return 0
    workflow = args.workflow_dir.resolve()
    workflow.mkdir(parents=True, exist_ok=False)
    state = {'status': 'running', 'phase': 'train', 'started_at': now(), 'llm_report': report,
             'workflow_dir': str(workflow), 'evaluation_scope': 'same fifteen training milestones; not held-out'}
    def save():
        write_json(workflow / 'workflow.json', state)
        write_json(ROOT / '_work/active-click15-workflow.json', state)
    write_json(workflow / 'llm-report.json', report)
    write_json(workflow / 'task-manifest.json', tasks)
    # Snapshot all inputs so later profile edits cannot change a queued phase.
    config_path = workflow / 'train-config.json'
    write_json(config_path, config)
    train_spec = {**read_json(TRAIN_SPEC), 'config': str(config_path), 'tasks': str(MANIFEST),
                  'swe_dataset': config['swe_dataset'], 'swe_split': config['swe_split'],
                  'label': f"click15-{config['model']['name']}-trace2skill-train"}
    train_spec_path = workflow / 'train-spec.json'
    write_json(train_spec_path, train_spec)
    save()
    try:
        if args.resume_training:
            training, completed = fork_training_checkpoint(args.resume_training, tasks, config.get('rollout_workers', 5),
                step_limit=config['step_limit'], rollout_timeout=config['rollout_timeout'],
                rate_limit_retry_delay=config['model'].get('rate_limit_retry_delay'))
            state.update(training_run=str(training), resumed_from=str(args.resume_training.resolve()),
                         preserved_completed_tasks=completed)
            save()
            code = monitor(resume=training, strict=True)
        else:
            code = monitor(benchmark_config=train_spec_path, strict=True)
        launch = read_json(ROOT / '_work/active-parallel-launch.json')
        training = Path(launch['output'])
        state['training_run'] = str(training)
        save()
        if code:
            raise RuntimeError('Training monitor stopped the run; evaluation will not start')
        spec, skill = freeze_training(training, tasks, workflow)
        state.update(phase='test', frozen_skill=str(skill))
        save()
        code = monitor(benchmark_config=spec, strict=True)
        state['test_run'] = read_json(ROOT / '_work/active-parallel-launch.json')['output']
        save()
        if code:
            raise RuntimeError('Frozen evaluation monitor stopped the run')
        evaluation = Path(state['test_run'])
        checkpoint = read_json(evaluation / 'checkpoint.json')
        if len(checkpoint['completed']) != 15 or checkpoint['current_skill_version'] != skill.name:
            raise ValueError('Evaluation incomplete or frozen Skill changed')
        if any(entry['update']['decision'] != 'fixed_skill' or entry['update']['accepted']
               or entry['report']['skill_version'] != skill.name for entry in checkpoint['completed']):
            raise ValueError('Evaluation did not retain the frozen Skill')
        state.update(status='completed', phase='completed', finished_at=now(),
                     metrics=read_json(evaluation / 'metrics.json'))
        save()
        return 0
    except BaseException as exc:
        state.update(status='stopped', error=f'{type(exc).__name__}: {exc}', stopped_at=now())
        save()
        raise


if __name__ == '__main__':
    raise SystemExit(main())
