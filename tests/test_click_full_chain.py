import tempfile
import unittest
from pathlib import Path

from rsi import ROOT
from rsi.core.common import read_json, write_json, digest
from rsi.core.config import load_config, model_configs
from rsi.skills.store import SkillStore, load_initial_skill
from scripts.run_click_full_chain import CONFIG, MANIFEST, freeze_training


class ClickFullChainTests(unittest.TestCase):
    def test_full_chain_and_model_roles(self):
        tasks = read_json(MANIFEST)
        self.assertEqual(len(tasks), 15)
        self.assertEqual([t['chain']['index'] for t in tasks], list(range(15)))
        config = load_config(CONFIG)
        self.assertEqual(config['step_limit'], 200)
        self.assertEqual(config['rollout_workers'], 5)
        self.assertEqual(config['rollout_timeout'], 1800)
        self.assertEqual(config['model']['name'], 'qwen3.8-flash')
        self.assertEqual(config['judge_model']['name'], 'qwen3.8-flash')
        self.assertEqual({config[k]['name'] for k in ('skill_error_model', 'skill_success_model', 'skill_editor_model')}, {'qwen3.8-max'})
        self.assertTrue(all(block['rate_limit_retry_delay'] == 60 for block in model_configs(config).values()))

    def prepare_training(self, root):
        tasks = read_json(MANIFEST)
        config = load_config(CONFIG)
        seed, _ = load_initial_skill()
        store = SkillStore(root / 'skills', seed)
        write_json(root / 'config.json', config)
        write_json(root / 'task-manifest.json', tasks)
        write_json(root / 'run-info.json', {'status': 'completed', 'mode': 'evolve', 'supervised_tasks': 15})
        checkpoint = {'current_skill_version': store.current().name, 'completed': [
            {'report': {'phase': 'train', 'supervision_complete': True}, 'update': {'decision': 'no_changes'}} for _ in tasks]}
        write_json(root / 'checkpoint.json', checkpoint)
        return tasks, checkpoint

    def test_freeze_requires_15_and_activates_only_flash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tasks, _ = self.prepare_training(root / 'train')
            spec_path, skill = freeze_training(root / 'train', tasks, root / 'workflow')
            spec = read_json(spec_path)
            config = load_config(spec['config'])
            self.assertFalse(config['evolve'])
            self.assertTrue(spec['fixed_skill'])
            self.assertEqual(spec['rollouts'], 1)
            self.assertFalse(spec['parallel'])
            self.assertEqual(config['execution_mode'], 'single')
            self.assertEqual(config['rollouts'], 1)
            self.assertEqual(config['rollout_workers'], 1)
            self.assertEqual(config['step_limit'], 200)
            self.assertEqual(config['rollout_timeout'], 1800)
            self.assertEqual(config['model']['temperature'], 0.0)
            self.assertEqual({b['name'] for b in model_configs(config, active_only=True).values()}, {'qwen3.8-flash'})
            self.assertEqual(spec['initial_skill'], str(skill))

    def test_failed_or_incomplete_training_never_freezes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tasks, checkpoint = self.prepare_training(root / 'train')
            checkpoint['completed'][0]['update']['decision'] = 'checks_failed'
            write_json(root / 'train/checkpoint.json', checkpoint)
            with self.assertRaisesRegex(ValueError, 'rejected or failed'):
                freeze_training(root / 'train', tasks, root / 'workflow')
            self.assertFalse((root / 'workflow/eval-spec.json').exists())
            checkpoint['completed'].pop()
            write_json(root / 'train/checkpoint.json', checkpoint)
            with self.assertRaisesRegex(ValueError, 'fifteen'):
                freeze_training(root / 'train', tasks, root / 'workflow')

    def test_freeze_luna_preserves_qwen_roles_and_frozen_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tasks, checkpoint = self.prepare_training(root / 'train')
            config = load_config(ROOT / 'configs/agent/gpt6-luna.yaml')
            write_json(root / 'train/config.json', config)
            spec_path, skill = freeze_training(root / 'train', tasks, root / 'workflow')
            frozen = load_config(read_json(spec_path)['config'])
            self.assertEqual(skill.name, checkpoint['current_skill_version'])
            self.assertFalse(frozen['evolve'])
            self.assertEqual((frozen['rollouts'], frozen['rollout_workers']), (1, 1))
            self.assertEqual(frozen['model'], config['model'] | {'temperature': 0.0})
            for role in ('judge_model', 'skill_error_model', 'skill_success_model', 'skill_editor_model'):
                self.assertEqual(frozen[role], config[role])
            self.assertEqual(frozen['initial_skill'], str(skill))

    def test_single_evaluation_uses_one_route_without_selector_or_learning(self):
        import threading
        from unittest.mock import patch
        from rsi.agent.runner import run_experiment
        from test_core_baseline import Backend, Models, test_config
        config = test_config()
        config.update(evolve=False, execution_mode='single', rollouts=1)
        Backend.barrier, Backend.instances = threading.Barrier(1), []
        models = Models()
        tasks = [{'id': f'one-route-{i}', 'benchmark': 'swe', 'prompt': 'Fix visible issue'} for i in range(2)]
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'eval'
            with patch('rsi.agent.runner.functional_gate', return_value={'passed': True}), \
                 patch('rsi.agent.runner.evolve_skill', side_effect=AssertionError('Frozen evaluation cannot learn')):
                reports = run_experiment(tasks, config, output, backend_factory=Backend, model_factory=models)
            self.assertEqual(len(Backend.instances), 2)
            self.assertEqual({role for role, _ in models.requests}, {'solver'})
            self.assertEqual(len({r['skill_version'] for r in reports}), 1)
            for task in tasks:
                folder = output / task['id']
                self.assertEqual(len(list(folder.glob('rollout-*/trajectory.json'))), 1)
                self.assertFalse((folder / 'selection.json').exists())
                self.assertEqual(read_json(folder / 'summary.json')['selector_usage']['calls'], 0)
                self.assertEqual(read_json(folder / 'update.json')['decision'], 'fixed_skill')

    def test_cli_frozen_flag_forces_single_route_from_legacy_five_route_config(self):
        from unittest.mock import patch
        from rsi.cli import main
        with patch('sys.argv', ['rsi', 'run', '--tasks', str(MANIFEST), '--config', str(CONFIG), '--fixed-skill']), \
             patch('rsi.cli.require_credentials', side_effect=RuntimeError('inspect config')) as credentials:
            with self.assertRaisesRegex(RuntimeError, 'inspect config'):
                main()
            config = credentials.call_args.args[0]
            self.assertEqual(config['rollouts'], 1)
            self.assertEqual(config['execution_mode'], 'single')
            self.assertFalse(config['evolve'])
            self.assertEqual(config['model']['temperature'], 0.0)

    def test_scheduler_only_resume_preserves_first_commit_and_source(self):
        from rsi.benchmarks.tasks import load_tasks
        from rsi.experiment.checkpoints import initialize, commit, resume_request
        from rsi.experiment.run_logs import create_run_directory
        from scripts.run_click_full_chain import fork_training_checkpoint
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = load_config(CONFIG)
            config.pop('rollout_workers')  # Original run used the five-worker default.
            config['step_limit'] = 0  # Stopped experiments had no step limit.
            for block in model_configs(config).values():
                block.pop('rate_limit_retry_delay')
            tasks = load_tasks(MANIFEST, config)
            files, metadata = load_initial_skill()
            source = create_run_directory(tasks, config, root / 'source', initial_skill=metadata)
            write_json(source / 'config.json', config)
            write_json(source / 'task-manifest.json', tasks)
            write_json(source / 'initial-skill-source.json', metadata)
            SkillStore(source / 'initial-skill', files)
            store = SkillStore(source / 'skills', files)
            initial = store.current().name
            candidate, _ = store.propose({'SKILL.md': files['SKILL.md'] + '\nPreserved learned procedure.\n'})
            state = initialize(source, tasks, config, initial)
            report = {'task_id': tasks[0]['id'], 'phase': 'train', 'selected': None, 'success': None,
                      'supervision_complete': True, 'graded_rollouts': list(range(5)), 'skill_version': initial}
            update = {'accepted': True, 'decision': 'checks_passed', 'before_skill_version': initial,
                      'after_skill_version': candidate.name}
            state = commit(source, state, report, update)
            write_json(source / tasks[0]['id'] / 'evidence-marker.json', {'preserve': True})
            nested = Path(tasks[0]['id']) / 'trace2skill/rollout-0/evaluations' / ('a' * 32) / 'logs/evaluation/rsi-abcdef123456/rsi' / tasks[0]['id'] / 'run_instance.log'
            (source / nested).parent.mkdir(parents=True, exist_ok=True)
            (source / nested).write_text('Preserved evaluation log', encoding='utf-8')
            write_json(source / tasks[1]['id'] / 'unfinished.json', {'old_attempt': True})
            old_checkpoint = (source / 'checkpoint.json').read_bytes()
            old_config = (source / 'config.json').read_bytes()
            target, completed = fork_training_checkpoint(source, read_json(MANIFEST), 3, runs_dir=root / 'runs')
            new_state, new_config, groups = resume_request(target)
            self.assertEqual(completed, 1)
            self.assertEqual(new_state['completed'], state['completed'])
            self.assertEqual(new_state['current_skill_version'], candidate.name)
            self.assertEqual(groups[0][completed]['id'], tasks[1]['id'])
            self.assertEqual(new_config, config | {'rollout_workers': 3})
            self.assertTrue((target / tasks[0]['id'] / 'evidence-marker.json').exists())
            self.assertEqual((target / nested).read_text(encoding='utf-8'), 'Preserved evaluation log')
            self.assertFalse((target / tasks[1]['id']).exists())
            self.assertTrue((source / tasks[1]['id'] / 'unfinished.json').exists())
            self.assertEqual((source / 'checkpoint.json').read_bytes(), old_checkpoint)
            self.assertEqual((source / 'config.json').read_bytes(), old_config)
            limited, completed = fork_training_checkpoint(source, read_json(MANIFEST), 5,
                step_limit=200, rollout_timeout=1800, rate_limit_retry_delay=60, runs_dir=root / 'runs')
            limited_state, limited_config, _ = resume_request(limited)
            self.assertEqual(completed, 1)
            self.assertEqual(limited_state['completed'], state['completed'])
            self.assertEqual(limited_state['current_skill_version'], candidate.name)
            expected = config | {'rollout_workers': 5, 'step_limit': 200, 'rollout_timeout': 1800}
            for role in model_configs(config):
                expected[role] = config[role] | {'rate_limit_retry_delay': 60}
            self.assertEqual(limited_config, expected)
            changes = read_json(limited / 'resume-source.json')['config_changes']
            self.assertEqual(changes['step_limit'], {'before': 0, 'after': 200})
            self.assertEqual(changes['rollout_workers'], {'before': 5, 'after': 5})
            self.assertEqual(changes['model.rate_limit_retry_delay'], {'before': None, 'after': 60})
            self.assertEqual((source / 'checkpoint.json').read_bytes(), old_checkpoint)
            self.assertEqual((source / 'config.json').read_bytes(), old_config)
            second_report = {**report, 'task_id': tasks[1]['id'], 'skill_version': candidate.name}
            second_update = {'accepted': False, 'decision': 'no_changes',
                             'before_skill_version': candidate.name, 'after_skill_version': candidate.name}
            prefix_state = commit(limited, limited_state, second_report, second_update)
            write_json(limited / 'task-manifest.json', tasks[:2])
            prefix_state['tasks_hash'] = digest(tasks[:2])
            write_json(limited / 'checkpoint.json', prefix_state)
            prefix_checkpoint = (limited / 'checkpoint.json').read_bytes()
            prefix_manifest = (limited / 'task-manifest.json').read_bytes()
            expanded, completed = fork_training_checkpoint(limited, read_json(MANIFEST), 2,
                step_limit=200, rollout_timeout=1800, runs_dir=root / 'runs')
            expanded_state, _, expanded_groups = resume_request(expanded)
            self.assertEqual(completed, 2)
            self.assertEqual(expanded_groups[0], tasks)
            self.assertEqual(expanded_state['completed'], prefix_state['completed'])
            self.assertEqual(expanded_state['current_skill_version'], candidate.name)
            self.assertEqual(expanded_groups[0][completed]['id'], tasks[2]['id'])
            self.assertTrue(all(task['prompt'] for task in expanded_groups[0]))
            extension = read_json(expanded / 'resume-source.json')['task_manifest_extension']
            self.assertEqual(extension['before'], 2)
            self.assertEqual(extension['added_task_ids'], [task['id'] for task in tasks[2:]])
            self.assertEqual((limited / 'checkpoint.json').read_bytes(), prefix_checkpoint)
            self.assertEqual((limited / 'task-manifest.json').read_bytes(), prefix_manifest)
