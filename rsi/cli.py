"""Run five-route Max agents with Trace2Skill or joint Skill aggregation."""
import argparse
import json
import subprocess
from pathlib import Path
from rsi import ROOT, UPSTREAM, DEPENDENCY_ROOT
from rsi.core.config import (DEFAULT_CONFIG, load_env, load_config, load_benchmark_config,
                             apply_benchmark_execution, require_credentials, credential_envs)
from rsi.core.common import read_json
from rsi.benchmarks.tasks import load_tasks, validate_environments
from rsi.frameworks import FRAMEWORKS, get_framework
from rsi.agent.rollouts import set_execution_mode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    run = commands.add_parser('run', aliases=list(FRAMEWORKS))
    source = run.add_mutually_exclusive_group(required=True)
    source.add_argument('--tasks', type=Path)
    source.add_argument('--benchmark-config', type=Path)
    source.add_argument('--resume', type=Path, help='Resume at the first uncommitted task using saved settings')
    run.add_argument('--config', type=Path)
    run.add_argument('--output', type=Path)
    run.add_argument('--runs-dir', type=Path)
    run.add_argument('--label')
    run.add_argument('--initial-skill', type=Path)
    run.add_argument('--fixed-skill', action='store_true')
    run.add_argument('--no-wandb', action='store_true')
    run.add_argument('--framework', choices=FRAMEWORKS)
    doctor = commands.add_parser('doctor')
    doctor.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    doctor.add_argument('--framework', choices=FRAMEWORKS)
    args = parser.parse_args()
    load_env()
    if args.command == 'doctor':
        import os
        config = load_config(args.config, framework=args.framework)
        get_framework(config).initialize(config)
        result = subprocess.run(['docker', 'info', '--format', '{{.ServerVersion}}'], capture_output=True, text=True)
        print(json.dumps({'root': str(ROOT), 'dependency_root': str(DEPENDENCY_ROOT),
                          'upstream_exists': UPSTREAM.exists(), 'docker_daemon': result.returncode == 0,
                          'skill_update_method': config['skill_update_method'],
                          'trace2skill_source_verified': config['skill_update_method'] == 'trace2skill',
                          'credentials': {name: bool(os.getenv(name)) for name in credential_envs(config)}}, indent=2))
        return
    framework = args.command if args.command in FRAMEWORKS else args.framework
    if args.command in FRAMEWORKS and args.framework and args.framework != args.command:
        parser.error('Framework command conflicts with --framework')
    if args.resume:
        if any((args.config, args.output, args.runs_dir, args.label, args.initial_skill,
                args.fixed_skill, args.no_wandb)):
            parser.error('--resume cannot be combined with configuration or output overrides')
        from rsi.experiment.checkpoints import resume_request
        from rsi.agent.runner import run_experiment
        from rsi.experiment.tracking import summary_metrics
        state, config, groups = resume_request(args.resume)
        if framework and framework != config['skill_update_method']:
            parser.error('Resume must use the saved framework')
        pending = groups[0][len(state['completed']):]
        if pending:
            require_credentials(config)
            validate_environments(pending, config)
        reports = run_experiment(groups[0], config, args.resume, resume=True)
        print(json.dumps(summary_metrics(reports), indent=2))
        return
    options = load_benchmark_config(args.benchmark_config) if args.benchmark_config else {}
    if options.get('test_tasks') or options.get('wandb'):
        raise ValueError('Core supports one explicit task manifest and local logging; no automatic workflow or W&B')
    config = load_config(args.config or options.get('config', DEFAULT_CONFIG), framework=framework)
    config['evolve'] = not (args.fixed_skill or options.get('fixed_skill', False))
    apply_benchmark_execution(config, options)
    for key in ('swe_dataset', 'swe_split', 'initial_skill'):
        if key in options:
            config[key] = options[key]
    if args.initial_skill:
        config['initial_skill'] = str(args.initial_skill.resolve())
    if not config['evolve']:
        set_execution_mode(config, 'single')
        config['model']['temperature'] = 0.0
    config['wandb'] = {'enabled': False}
    require_credentials(config)
    tasks = load_tasks(args.tasks or options['tasks'], config)
    validate_environments(tasks, config)
    from rsi.agent.runner import run_experiment
    from rsi.experiment.tracking import summary_metrics
    reports = run_experiment(tasks, config, args.output or options.get('output'),
                             runs_dir=args.runs_dir or options.get('runs_dir'), label=args.label or options.get('label'))
    print(json.dumps(summary_metrics(reports), indent=2))


if __name__ == '__main__':
    main()
