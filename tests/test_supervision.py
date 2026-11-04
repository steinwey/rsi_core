import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from rsi import ROOT
from rsi.agent.runner import run_experiment
from rsi.core.common import read_json, write_json
from rsi.core.config import load_config
from rsi.skills.supervision import MAX_FAILURE_CHARS, MAX_TEST_NAMES, build_supervision, build_all_supervision
from test_core_baseline import Backend, Models


class SupervisionTests(unittest.TestCase):
    def setUp(self):
        evaluator = patch('rsi.skills.trace2skill.validation.formal_score', side_effect=__import__('formal_helpers').formal_pass)
        evaluator.start()
        self.addCleanup(evaluator.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.task = {'id': 'example', 'benchmark': 'swe', 'prompt': 'Fix the issue'}
        self.results = [{'rollout': i, 'artifact': f'patch-{i}'} for i in range(5)]

    def report(self, *, success=False):
        path = self.root / 'report.json'
        write_json(path, {'example': {'resolved': success, 'infra_failure': False,
            'patch_successfully_applied': True,
            'reference_patch': 'REFERENCE_MUST_NOT_BE_INCLUDED',
            'tests_status': {'FAIL_TO_PASS': {'success': [], 'failure': ['test_contract']},
                             'PASS_TO_PASS': {'success': ['test_old'], 'failure': ['test_regression']}}}})
        path.with_name('test_output.txt').write_text(
            'noise\n' * 10 + '________ test_contract ________\nassert option.default is True\n'
            'E       AssertionError: wrapper is not True\nFAILED test_contract\n', encoding='utf-8')
        return {'success': success, 'report': str(path)}

    def test_failure_labels_assertions_and_reference_exclusion(self):
        score = self.report()
        original = copy.deepcopy(score)
        feedback = build_supervision(self.task, self.results, 2, score)
        self.assertEqual(score, original)
        self.assertFalse(feedback['grade']['success'])
        self.assertTrue(feedback['grade']['patch_successfully_applied'])
        self.assertEqual(feedback['grade']['tests_status']['FAIL_TO_PASS']['failure']['tests'], ['test_contract'])
        self.assertIn('assert option.default is True', feedback['failure_output']['text'])
        self.assertIn('wrapper is not True', feedback['failure_output']['text'])
        self.assertNotIn('REFERENCE_MUST_NOT_BE_INCLUDED', json.dumps(feedback))
        self.assertGreater(feedback['failure_output']['omitted_chars'], 0)
        for route in feedback['routes']:
            self.assertEqual(route['graded'], route['rollout'] == 2)
            self.assertIs(route['success'], False if route['rollout'] == 2 else None)

    def test_success_and_missing_diagnostics_remain_explicit(self):
        score = self.report(success=True)
        (self.root / 'test_output.txt').unlink()
        feedback = build_supervision(self.task, self.results, 0, score)
        self.assertTrue(feedback['grade']['success'])
        self.assertFalse(feedback['failure_output']['available'])
        feedback = build_supervision(self.task, self.results, 0, {'success': False, 'reason': 'empty_patch', 'hidden': 'secret'})
        self.assertEqual(feedback['grade'], {'success': False, 'reason': 'empty_patch'})
        self.assertNotIn('secret', json.dumps(feedback))

    def test_infrastructure_mismatch_and_unknown_route_are_rejected(self):
        for score in ({'success': None}, {'success': False, 'infra_failure': True}):
            with self.assertRaises(ValueError):
                build_supervision(self.task, self.results, 0, score)
        with self.assertRaises(ValueError):
            build_supervision(self.task, self.results, 6, {'success': False})
        score = self.report()
        score['success'] = True
        with self.assertRaisesRegex(ValueError, 'disagrees'):
            build_supervision(self.task, self.results, 0, score)

    def test_bounds_preserve_counts_and_identify_truncation(self):
        score = self.report()
        report = read_json(score['report'])
        names = [f'test_{i}' for i in range(MAX_TEST_NAMES + 7)]
        report['example']['tests_status']['FAIL_TO_PASS']['failure'] = names
        write_json(score['report'], report)
        (self.root / 'test_output.txt').write_text('E       AssertionError: ' + 'x' * (MAX_FAILURE_CHARS + 100), encoding='utf-8')
        feedback = build_supervision(self.task, self.results, 0, score)
        failures = feedback['grade']['tests_status']['FAIL_TO_PASS']['failure']
        self.assertEqual(failures['count'], MAX_TEST_NAMES + 7)
        self.assertEqual(len(failures['tests']), MAX_TEST_NAMES)
        self.assertEqual(failures['omitted'], 7)
        self.assertEqual(len(feedback['failure_output']['text']), MAX_FAILURE_CHARS)
        self.assertTrue(feedback['failure_output']['truncated'])

    def test_all_route_feedback_keeps_independent_grades_and_requires_complete_set(self):
        scores = {i: {'success': i == 0} for i in range(5)}
        feedback = build_all_supervision(self.task, self.results, 4, scores)
        self.assertEqual(feedback['schema'], 2)
        self.assertFalse(feedback['grade']['success'])
        self.assertEqual([r['success'] for r in feedback['routes']], [True, False, False, False, False])
        self.assertTrue(all(r['graded'] for r in feedback['routes']))
        self.assertEqual(len({r['artifact_sha256'] for r in feedback['routes']}), 5)
        with self.assertRaises(ValueError):
            build_all_supervision(self.task, self.results, 4, {i: scores[i] for i in range(4)})
        scores[2] = {'success': None}
        with self.assertRaises(ValueError):
            build_all_supervision(self.task, self.results, 4, scores)

    def test_accuracy_uses_only_preselected_route_with_mixed_outcomes(self):
        config = load_config(ROOT / 'configs/agent/qwen.yaml')
        for role, name in (('model', 'solver'), ('judge_model', 'judge'), ('skill_editor_model', 'judge'),
                           ('skill_error_model', 'error_analyst'), ('skill_success_model', 'success_analyst')):
            config[role]['name'] = name
        config['evolve'] = False
        for selected_passes in (False, True):
            calls = []
            def score(backend, artifact):
                self.assertTrue((backend.folder.parent / 'selection.json').exists())
                route = int(backend.folder.name.split('-')[-1])
                self.assertEqual(artifact, f'patch-rollout-{route}')
                calls.append(route)
                return {'success': (route == 4) if selected_passes else (route == 0)}
            Backend.barrier, Backend.instances = threading.Barrier(5), []
            output = self.root / str(selected_passes)
            with patch('rsi.agent.runner.functional_gate', return_value={'passed': True}), \
                 patch.object(Backend, 'score', score):
                run_experiment([self.task], config, output, backend_factory=Backend, model_factory=Models())
            self.assertEqual(calls, [4])
            self.assertEqual(read_json(output / 'example/score.json')['success'], selected_passes)
            self.assertEqual(read_json(output / 'metrics.json')['accuracy'], float(selected_passes))
            self.assertFalse((output / 'example/supervision.json').exists())
            self.assertFalse((output / 'example/trace2skill').exists())

    def test_unselected_grading_error_prevents_update_and_checkpoint(self):
        config = load_config(ROOT / 'configs/agent/qwen.yaml')
        for role, name in (('model', 'solver'), ('judge_model', 'judge'), ('skill_editor_model', 'judge'),
                           ('skill_error_model', 'error_analyst'), ('skill_success_model', 'success_analyst')):
            config[role]['name'] = name
        def score(backend, artifact):
            if backend.folder.name == 'rollout-0':
                raise RuntimeError('grading infrastructure error')
            return {'success': True}
        Backend.barrier, Backend.instances = threading.Barrier(5), []
        output = self.root / 'failed-run'
        with patch('rsi.agent.runner.functional_gate', return_value={'passed': True}), \
             patch.object(Backend, 'score', score), \
             patch('rsi.agent.runner.evolve_skill', side_effect=AssertionError('Must not update')), \
             self.assertRaisesRegex(RuntimeError, 'rollout-0'):
            run_experiment([self.task], config, output, backend_factory=Backend, model_factory=Models())
        self.assertEqual(read_json(output / 'checkpoint.json')['completed'], [])
        self.assertFalse((output / 'example/supervision.json').exists())
        self.assertFalse((output / 'example/score.json').exists())
        self.assertIsNone(read_json(output / 'example/rollout-0/score.json')['success'])

    def test_empty_training_patches_still_supply_five_failure_labels(self):
        from test_core_baseline import test_config
        Backend.barrier, Backend.instances = threading.Barrier(5), []
        output = self.root / 'empty-training'
        with patch('rsi.agent.runner.functional_gate', return_value={'passed': True}), \
             patch.object(Backend, 'artifact', return_value=''), \
             patch.object(Backend, 'score', return_value={'success': False, 'reason': 'empty_patch'}) as grade, \
             patch('rsi.agent.runner.evolve_skill', return_value={'changes': {}, 'reason': 'No supported lesson'}) as evolve:
            run_experiment([self.task], test_config(), output, backend_factory=Backend, model_factory=Models())
        self.assertEqual(grade.call_count, 5)
        feedback = evolve.call_args.kwargs['supervision']
        self.assertIsNone(feedback['selected_rollout'])
        self.assertNotIn('grade', feedback)
        self.assertTrue(all(r['success'] is False for r in feedback['routes']))
        self.assertEqual(read_json(output / 'run-info.json')['status'], 'completed')
        self.assertEqual(read_json(output / 'metrics.json')['supervised_routes'], 5)

    def test_feedback_reaches_only_trace2skill_and_is_checkpointed_after_false_score(self):
        config = load_config(ROOT / 'configs/agent/qwen.yaml')
        for role, name in (('model', 'solver'), ('judge_model', 'judge'), ('skill_editor_model', 'judge'),
                           ('skill_error_model', 'error_analyst'), ('skill_success_model', 'success_analyst')):
            config[role]['name'] = name
        Backend.barrier, Backend.instances = threading.Barrier(5), []
        models = Models()
        score = self.report()
        output = self.root / 'run'
        with patch('rsi.agent.runner.functional_gate', return_value={'passed': True}), \
             patch.object(Backend, 'score', return_value=score):
            reports = run_experiment([self.task], config, output, backend_factory=Backend, model_factory=models)
        self.assertIsNone(reports[0]['success'])
        self.assertEqual(len(read_json(output / 'checkpoint.json')['completed']), 1)
        feedback = read_json(output / 'example/supervision.json')
        self.assertFalse((output / 'example/selector.request.json').exists())
        learning_input = read_json(output / 'example/trace2skill/input.json')
        self.assertEqual(learning_input['supervision'], feedback)
        self.assertIn('wrapper is not True', json.dumps(learning_input))
        self.assertNotIn('wrapper is not True', json.dumps([m for n, m in models.requests if n in ('solver', 'reviewer')]))
        update = read_json(output / 'example/update.json')
        self.assertTrue(update['supervision']['complete'])
        self.assertNotIn('success', update['supervision'])
        self.assertNotIn('accuracy', read_json(output / 'metrics.json'))
        self.assertEqual(read_json(output / 'run-info.json')['supervised_tasks'], 1)
        self.assertFalse(any(m[0]['content'].startswith('Select') for _, m in models.requests))
        committed = read_json(output / 'checkpoint.json')['completed'][0]['update']
        self.assertEqual(committed['supervision'], update['supervision'])


if __name__ == '__main__':
    unittest.main()
