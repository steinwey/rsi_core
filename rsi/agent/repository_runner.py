"""Opt-in repository-scoped online Trace2Skill stream, separate from legacy checkpoints."""
from __future__ import annotations

import hashlib
from pathlib import Path

from rsi.agent.runner import run_group
from rsi.agent.execution import progress
from rsi.benchmarks.backends import make_backend
from rsi.core.common import digest, read_json, write_json, safe_id
from rsi.core.model import Model
from rsi.experiment.run_logs import create_run_directory, update_run
from rsi.experiment.checkpoints import run_lock
from rsi.frameworks import evolve_skill, get_framework
from rsi.skills.directory import directory_schema, read_skill_directory
from rsi.skills.maintenance import settings as maintenance_settings, maintain_candidate
from rsi.skills.repository_skill import RepositoryPool, options, task_repo, route_task
from rsi.skills.store import (SkillStore, SkillProposalRejected, document_gate,
                              functional_gate, load_initial_skill)
from rsi.skills.supervision import build_all_supervision


def run_repository_experiment(tasks, config, output=None, *, runs_dir=None, label=None,
                              resume=False, backend_factory=make_backend,
                              model_factory=Model, counter_factory=None):
    """Per-repository checkpoints, with one immutable task route per five-rollout group."""
    if not options(config)['enabled']:
        raise ValueError('repository_skill must be enabled')
    from rsi.agent.rollouts import validate_execution_mode
    from rsi.agent.action_protocol import validate_action_protocol
    from rsi.benchmarks.evo_swe import validate_order
    validate_execution_mode(config)
    validate_action_protocol(config)
    validate_order(tasks)
    if config['evolve']:
        get_framework(config).initialize(config)
    if not tasks or len({t['id'] for t in tasks}) != len(tasks):
        raise ValueError('Nonempty tasks must have unique IDs')
    repos = [task_repo(t) for t in tasks]
    maintenance = maintenance_settings(config)
    schema = directory_schema(config)
    if resume:
        output = Path(output).resolve()
        if read_json(output / 'config.json') != config or read_json(output / 'task-manifest.json') != tasks:
            raise ValueError('Repository stream resume requires unchanged tasks and config')
        state = read_json(output / 'repository-state.json')
        if state.get('schema') != 1 or not isinstance(state.get('completed'), list):
            raise ValueError('Invalid repository stream state')
        seed_version = read_json(output / 'initial-skill/current.json')['version']
        seed_files = read_skill_directory(output / 'initial-skill/versions' / seed_version,
                                          directory_schema=1)
        seed = SkillStore(output / 'initial-skill', seed_files, directory_schema=1, maintenance=maintenance)
    else:
        seed_files, source = load_initial_skill(config.get('initial_skill'), directory_schema=schema, bootstrap=True)
        output = create_run_directory(tasks, config, output, runs_dir=runs_dir, label=label, initial_skill=source)
        write_json(output / 'config.json', config)
        write_json(output / 'task-manifest.json', tasks)
        write_json(output / 'initial-skill-source.json', source)
        if maintenance['enabled']:
            outcome = maintain_candidate(seed_files, {}, config, output / 'bootstrap-maintenance', model_factory=model_factory)
            seed_files = outcome['files']
        seed = SkillStore(output / 'initial-skill', seed_files, directory_schema=1, maintenance=maintenance)
        state = {'schema': 1, 'completed': []}
        write_json(output / 'repository-state.json', state)
    with run_lock(output):
        pool = RepositoryPool(output / 'repository-skills', seed.current(), maintenance=maintenance)
        report_list = [e['report'] for e in state['completed']]
        if len(report_list) > len(tasks):
            raise ValueError('Repository stream state longer than task manifest')
        # Recover only fully persisted task commit receipts; never re-run an already learned item.
        for number, task in enumerate(tasks):
            if number >= len(state['completed']):
                break
            entry = state['completed'][number]
            if entry['report']['task_id'] != task['id'] or entry['repo'] != repos[number]:
                raise ValueError('Repository task order changed in checkpoint')
            store = pool.store(entry['repo'])
            if not (store.root / 'versions' / entry['after_version']).is_dir():
                raise ValueError('Committed repository version missing')
        # An interrupted pointer update requires deterministic reconciliation.
        while len(state['completed']) < len(tasks):
            i = len(state['completed'])
            task, repo = tasks[i], repos[i]
            location = output / safe_id(task['id'])
            pending = location / 'repository-commit.json'
            if not pending.is_file():
                break
            saved = read_json(pending)
            store = pool.store(repo)
            if (saved['task_id'] != task['id'] or saved['repo'] != repo
                    or saved['after_version'] != store.current().name):
                raise ValueError('Interrupted repository commit requires manual reconciliation')
            state['completed'].append(saved['entry'])
            write_json(output / 'repository-state.json', state)
        for index in range(len(state['completed']), len(tasks)):
            task = tasks[index]
            repo = repos[index]
            store = pool.store(repo)
            skill = store.current()
            folder = output / safe_id(task['id'])
            if folder.exists():
                # Never delete unfinished trajectories or ambiguity about a half-written update.
                raise ValueError(f'Uncommitted task folder must be archived before replay: {folder}')
            folder.mkdir(parents=True)
            gate = document_gate(skill, store.fixed_files, directory_schema=1, maintenance=store.maintenance)
            if not gate['passed']:
                raise RuntimeError(gate['error'])
            progress(f'[{index + 1}/{len(tasks)}] {task["id"]} repository={repo} source={skill.name}')
            routed, runtime, receipt = route_task(task, pool, seed_files, skill, config, folder,
                                                  model_factory=model_factory, counter_factory=counter_factory)
            report, results, evidence = run_group(
                task, folder, skill, config, backend_factory=backend_factory,
                model_factory=model_factory, counter_factory=counter_factory,
                task_route=routed, runtime_skill=runtime)
            report['repository_skill'] = {'source_repo': repo, 'candidate_version': skill.name,
                                          'route_id': receipt['route_id'],
                                          'source_skill_versions': receipt['source_skill_versions']}
            report['curator_usage'] = receipt['curator_usage']
            for key in ('input_tokens', 'output_tokens'):
                report[key] += receipt['curator_usage'].get(key, 0)
            report['tokens'] = report['input_tokens'] + report['output_tokens']
            update = {'accepted': False, 'decision': 'fixed_skill'}
            candidate = skill
            if config['evolve']:
                outcomes = read_json(folder / 'all-route-scores.json')['scores']
                scores = {item['rollout']: {k: v for k,v in item.items() if k != 'rollout'} for item in outcomes}
                supervision = build_all_supervision(task, results, report.get('selected'), scores)
                write_json(folder / 'supervision.json', supervision)
                proposal = evolve_skill(task, results, skill, config, model_factory, folder=folder,
                                        supervision=supervision, prepared_evidence=evidence)
                completion = proposal.get('completion') or ('complete' if proposal['changes'] else 'no_changes')
                if proposal['changes']:
                    try:
                        changes, managed = proposal['changes'], None
                        if maintenance['enabled']:
                            changed = maintain_candidate(read_skill_directory(skill, directory_schema=1), changes,
                                config, folder / 'maintenance', model_factory=model_factory)
                            changes, managed = changed['changes'], changed['managed_changes']
                        candidate, diff = store.propose(changes, managed_changes=managed,
                                                        expected_base_version=skill.name)
                        gate = document_gate(candidate, store.fixed_files, check_consistency=True,
                                             directory_schema=1, maintenance=maintenance)
                        accepted = gate['passed'] and candidate != skill
                        decision = 'unchanged_candidate' if not accepted else (
                            'partial_write' if completion == 'partial' else 'document_checks_passed')
                        update = {'accepted': accepted, 'decision': decision, 'completion': completion,
                                  'reason': proposal['reason'], 'evidence_rollouts': proposal['evidence_rollouts'],
                                  'document_gate': gate}
                        (folder / 'skill.diff').write_text(diff, encoding='utf-8')
                    except SkillProposalRejected as exc:
                        update = {'accepted': False, 'decision': 'checks_failed', 'reason': str(exc)}
                        candidate = skill
                elif completion == 'partial':
                    update = {'accepted': False, 'decision': 'partial_write', 'completion': completion,
                              'reason': proposal['reason']}
                else:
                    update = {'accepted': False, 'decision': 'no_changes', 'completion': completion,
                              'reason': proposal['reason']}
            for usage_file in (folder / 'trace2skill/usage.json', folder / 'maintenance/usage.json'):
                if usage_file.is_file():
                    item = read_json(usage_file)
                    totals = item.get('total', item)
                    for key in ('input_tokens', 'output_tokens'):
                        report[key] += totals.get(key, 0)
                    report['tokens'] = report['input_tokens'] + report['output_tokens']
            after = candidate.name if update['accepted'] else skill.name
            update.update(before_skill_version=skill.name, after_skill_version=after,
                          source_repo=repo, training_run_id=output.name)
            entry = {'repo': repo, 'before_version': skill.name, 'after_version': after,
                     'report': report, 'update': update}
            write_json(folder / 'summary.json', report)
            write_json(folder / 'update.json', update)
            # Persist a durable commit intent before advancing the per-repo pointer.
            write_json(folder / 'repository-commit.json', {
                'task_id': task['id'], 'repo': repo, 'after_version': after, 'entry': entry})
            pool.record(repo, store, candidate if update['accepted'] else skill,
                        update['accepted'], output.name + '/' + safe_id(task['id']),
                        {'task_id': task['id'], 'decision': update['decision']})
            state['completed'].append(entry)
            write_json(output / 'repository-state.json', state)
            report_list.append(report)
            write_json(output / 'summary.json', report_list)
            update_run(output, completed_tasks=len(report_list),
                       scored_tasks=sum(e['report']['phase'] == 'test' for e in state['completed']))
        update_run(output, status='completed')
        return [entry['report'] for entry in state['completed']]
