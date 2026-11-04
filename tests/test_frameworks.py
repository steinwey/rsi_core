import copy
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from rsi import ROOT
from rsi.agent.runner import run_experiment
from rsi.core.common import read_json
from rsi.core.config import load_config
from rsi.experiment.checkpoints import resume_request
from rsi.frameworks import get_framework
from test_core_baseline import Backend, Models, test_config


class BankModels(Models):
    def __init__(self, *, invalid=False, empty=False, interrupt=False, replies=None):
        super().__init__()
        self.aggregations = []
        self.invalid, self.empty, self.interrupt = invalid, empty, interrupt
        self.replies = list(replies or [])

    def __call__(self, config):
        if config['name'] != 'bank':
            return super().__call__(config)
        owner = self
        class Client:
            def __init__(self):
                self.calls = 0

            def query(self, messages):
                self.calls += 1
                owner.requests.append(('bank', copy.deepcopy(messages)))
                data = json.loads(messages[1]['content'])
                owner.aggregations.append(data)
                if owner.interrupt:
                    raise KeyboardInterrupt('stop aggregation')
                skill = data['current_skill']['SKILL.md'] + '\nVerify caller contracts using independent state and output assertions.\n'
                sources = [5] if owner.invalid else [0, 1]
                answer = {'reason': 'Contrast shared evidence and grade limitations.',
                          'skill': None if owner.empty else skill,
                          'source_rollouts': [] if owner.empty else sources}
                if owner.replies:
                    transform = owner.replies.pop(0)
                    if isinstance(transform, Exception):
                        raise transform
                    answer = transform(answer) if callable(transform) else transform
                return {'content': answer if isinstance(answer, str) else json.dumps(answer)}

            def stats(self):
                return {'calls': self.calls, 'input_tokens': 10*self.calls,
                        'output_tokens': 5*self.calls, 'cost_usd': 0}
        return Client()


class FrameworkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / 'run'
        self.config = test_config()
        self.config['skill_update_method'] = 'reasoningbank'
        self.config['skill_editor_model']['name'] = 'bank'
        self.tasks = [{'id': f'example-{i}', 'benchmark': 'swe', 'prompt': 'Fix caller behavior'} for i in range(2)]
        Backend.barrier, Backend.instances = threading.Barrier(5), []
        gate = patch('rsi.agent.runner.functional_gate', return_value={'passed': True})
        gate.start()
        self.addCleanup(gate.stop)
        upstream = patch('rsi.frameworks.trace2skill.initialize', side_effect=AssertionError('Trace2Skill must be isolated'))
        upstream.start()
        self.addCleanup(upstream.stop)

    def run_tasks(self, models, *, tasks=None, resume=False):
        return run_experiment(tasks or self.tasks, self.config, self.output, resume=resume,
                              backend_factory=Backend, model_factory=models)

    def test_aggregate_skill_inherit_account_and_resume(self):
        models = BankModels()
        # Supply both positive and negative supervision for contrast.
        with patch.object(Backend, 'score', lambda backend, artifact: {'success': artifact.endswith('0')}):
            reports = self.run_tasks(models)
        self.assertEqual(len(models.aggregations), 2)
        first = models.aggregations[0]
        self.assertEqual(len(first['trajectories']), 5)
        self.assertEqual({r['success'] for r in first['supervision']['routes']}, {True, False})
        self.assertNotIn('reference_patch', first)
        state, config, _ = resume_request(self.output)
        skill = self.output / 'skills/versions' / state['current_skill_version']
        guide = (skill / 'SKILL.md').read_text(encoding='utf-8')
        self.assertIn('Verify caller contracts', guide)
        self.assertFalse((skill / 'references/reasoningbank.md').exists())
        route = read_json(self.output / 'example-1/rollout-0/trajectory.json')
        self.assertIn('Verify caller contracts', route['messages'][0]['content'])
        self.assertIn('Verify caller contracts', models.aggregations[1]['current_skill']['SKILL.md'])
        self.assertFalse(list(self.output.glob('*/reasoningbank/retrieval.json')))
        self.assertEqual(sum(r['input_tokens'] for r in reports), 10 * len(models.requests))
        update = read_json(self.output / 'example-0/update.json')
        self.assertEqual(update['method'], 'reasoningbank')
        self.assertEqual(update['reasoningbank_usage']['calls'], 1)
        self.assertFalse(list(self.output.glob('*/trace2skill')))
        before = len(models.requests)
        self.run_tasks(models, resume=True)
        self.assertEqual(len(models.requests), before)

    def test_fixed_skill_injects_without_learning(self):
        self.run_tasks(BankModels(), tasks=self.tasks[:1])
        self.config['initial_skill'] = str(self.output)
        self.config['evolve'] = False
        trained = self.output
        self.output = Path(self.temp.name) / 'eval'
        models = BankModels()
        self.run_tasks(models, tasks=self.tasks[1:])
        self.assertFalse(models.aggregations)
        self.assertEqual(read_json(self.output / 'example-1/update.json')['decision'], 'fixed_skill')
        route = read_json(self.output / 'example-1/rollout-0/trajectory.json')
        self.assertIn('Verify caller contracts', route['messages'][0]['content'])
        self.assertFalse((self.output / 'example-1/reasoningbank').exists())
        self.assertEqual(read_json(trained / 'skills/current.json'), read_json(self.output / 'skills/current.json'))

    def test_exhausted_repairs_keep_old_skill_and_commit_supervision(self):
        models = BankModels(invalid=True)
        self.run_tasks(models, tasks=self.tasks[:1])
        self.assertEqual(len(models.aggregations), 4)  # Initial proposal + three repairs.
        update = read_json(self.output / 'example-0/update.json')
        self.assertEqual(update['decision'], 'checks_failed')
        self.assertFalse(update['accepted'])
        self.assertEqual(update['before_skill_version'], update['after_skill_version'])
        self.assertEqual(update['reasoningbank_usage']['calls'], 4)
        self.assertEqual(read_json(self.output / 'example-0/reasoningbank/summary.json')['status'], 'rejected')
        self.assertEqual(len(read_json(self.output / 'checkpoint.json')['completed']), 1)
        self.run_tasks(models, tasks=self.tasks[:1], resume=True)
        self.assertEqual(len(models.aggregations), 4)

    def test_max_repairs_metadata_before_skill_is_adopted(self):
        def break_metadata(answer):
            return {**answer, 'skill': answer['skill'].replace('name: test-and-diagnose', 'name: invalid')}
        models = BankModels(replies=[break_metadata])
        self.run_tasks(models, tasks=self.tasks[:1])
        self.assertEqual(len(models.aggregations), 2)
        repair = models.aggregations[1]
        self.assertIn('preserve Skill metadata', repair['validation_error'])
        self.assertIn('name: invalid', repair['previous_response']['skill'])
        self.assertEqual(repair['trajectories'], models.aggregations[0]['trajectories'])
        self.assertEqual(repair['current_skill'], models.aggregations[0]['current_skill'])
        update = read_json(self.output / 'example-0/update.json')
        self.assertTrue(update['accepted'])
        self.assertEqual(update['reasoningbank_usage']['calls'], 2)
        state, _, _ = resume_request(self.output)
        text = (self.output / 'skills/versions' / state['current_skill_version'] / 'SKILL.md').read_text()
        self.assertNotIn('name: invalid', text)
        self.assertEqual([a['passed'] for a in read_json(self.output / 'example-0/reasoningbank/validation.json')['attempts']], [False, True])

    def test_malformed_json_is_sent_back_for_repair(self):
        models = BankModels(replies=['not JSON'])
        self.run_tasks(models, tasks=self.tasks[:1])
        self.assertEqual(models.aggregations[1]['previous_response'], 'not JSON')
        self.assertTrue(read_json(self.output / 'example-0/update.json')['accepted'])
        self.assertTrue((self.output / 'example-0/reasoningbank/aggregate.error.json').exists())

    def test_store_check_failures_are_repaired_before_submission(self):
        def unknown_tool(answer):
            return {**answer, 'skill': answer['skill'] + '\nRun scripts/nonexistent.py\n'}
        models = BankModels(replies=[unknown_tool])
        self.run_tasks(models, tasks=self.tasks[:1])
        self.assertIn('Unknown fixed tool reference', models.aggregations[1]['validation_error'])
        self.assertTrue(read_json(self.output / 'example-0/update.json')['accepted'])

    def test_transport_error_is_not_treated_as_a_document_repair(self):
        models = BankModels(replies=[RuntimeError('API unavailable')])
        with self.assertRaisesRegex(RuntimeError, 'API unavailable'):
            self.run_tasks(models, tasks=self.tasks[:1])
        self.assertEqual(len(models.aggregations), 1)
        self.assertEqual(read_json(self.output / 'checkpoint.json')['completed'], [])

    def test_repair_can_decline_unsupported_update(self):
        models = BankModels(replies=['invalid JSON', {'reason': 'Insufficient evidence', 'skill': None, 'source_rollouts': []}])
        self.run_tasks(models, tasks=self.tasks[:1])
        self.assertEqual(read_json(self.output / 'example-0/update.json')['decision'], 'no_changes')

    def test_empty_aggregation_keeps_version(self):
        self.run_tasks(BankModels(empty=True), tasks=self.tasks[:1])
        update = read_json(self.output / 'example-0/update.json')
        self.assertEqual(update['decision'], 'no_changes')
        self.assertEqual(update['before_skill_version'], update['after_skill_version'])

    def test_config_backbone_and_framework_override(self):
        for method in ('trace2skill', 'reasoningbank'):
            config = load_config(ROOT / 'configs/agent/qwen.yaml', framework=method)
            self.assertEqual(config['skill_update_method'], method)
            for role in ('model', 'judge_model', 'skill_editor_model', 'skill_error_model', 'skill_success_model'):
                self.assertEqual(config[role]['name'], 'qwen3.8-max')
            self.assertEqual(get_framework(config).__name__, 'rsi.frameworks.' + method)
        with self.assertRaises(ValueError):
            load_config(ROOT / 'configs/agent/qwen.yaml', framework='unknown')

    def test_cli_dispatch_and_resume_mode_conflicts(self):
        from rsi.cli import main
        for method in ('trace2skill', 'reasoningbank'):
            with patch.object(sys, 'argv', ['rsi', method, '--tasks', 'tasks.json']), \
                 patch('rsi.cli.require_credentials', side_effect=RuntimeError('configured')) as credentials:
                with self.assertRaisesRegex(RuntimeError, 'configured'):
                    main()
                self.assertEqual(credentials.call_args.args[0]['skill_update_method'], method)
        with patch.object(sys, 'argv', ['rsi', 'trace2skill', '--framework', 'reasoningbank', '--tasks', 'tasks.json']):
            with self.assertRaises(SystemExit):
                main()


if __name__ == '__main__':
    unittest.main()
