import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from rsi.agent.runner import run_experiment
from rsi.core.common import read_json
from rsi.skills.store import SkillProposalRejected
from rsi.skills.trace2skill.evidence import EvidenceReader
from rsi.skills.trace2skill.upstream import load
from scripts.monitor_experiment import activity_paths, problem_in
from test_core_baseline import Backend, Models, test_config


class Trace2SkillTests(unittest.TestCase):
    def test_budget_exhaustion_gets_evidence_only_finalization(self):
        class InvestigatingModels(Models):
            def __call__(owner, config):
                client = super().__call__(config)
                query = client.query
                def respond(messages, **kwargs):
                    if client.name == 'error_analyst' and '[System Check] Turn budget exhausted' not in messages[-1]['content']:
                        client.n_calls += 1
                        return {'content': 'Action:\n{"name":"read_evidence","arguments":{"kind":"patch"}}'}
                    return query(messages, **kwargs)
                client.query = respond
                return client
        config = test_config()
        config['trace2skill']['analysis_max_turns'] = 1
        with patch.object(Backend, 'score', return_value={'success': False}):
            self.run_task(InvestigatingModels(), config)
        for i in range(5):
            path = self.output / f'example/trace2skill/rollout-{i}'
            self.assertTrue((path / 'budget-finalization.json').exists())
            self.assertTrue((path / 'analysis.json').exists())

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / 'run'
        self.task = {'id': 'example', 'benchmark': 'swe', 'prompt': 'Fix the visible issue'}
        Backend.barrier, Backend.instances = threading.Barrier(5), []

    def run_task(self, models, config=None):
        with patch('rsi.agent.runner.functional_gate', return_value={'passed': True}), \
             patch('rsi.skills.trace2skill.validation.formal_score', side_effect=__import__('formal_helpers').formal_pass):
            return run_experiment([self.task], config or test_config(), self.output,
                                  backend_factory=Backend, model_factory=models)

    def test_mixed_routes_use_max_analysis_and_actual_upstream_evolution(self):
        with patch.object(Backend, 'score', lambda backend, _: {'success': backend.folder.name in ('rollout-0', 'rollout-4')}):
            reports = self.run_task(Models())
        folder = self.output / 'example/trace2skill'
        self.assertIsNone(reports[0]['success'])
        usage = read_json(folder / 'usage.json')
        roles = [item['role'] for item in usage['clients']]
        self.assertEqual(roles.count('skill_success_model'), 2)
        self.assertEqual(roles.count('skill_error_model'), 3)
        self.assertEqual(roles.count('skill_editor_model'), 1)
        # These are produced by the imported official MAP and REDUCE implementation.
        self.assertTrue((folder / 'patches/map_patches').exists())
        self.assertTrue((folder / 'patches/merge_level_1').exists())
        self.assertTrue((folder / 'patches').exists())
        self.assertEqual(len(read_json(folder / 'analysis-records.json')), 5)
        self.assertEqual(read_json(folder / 'summary.json')['status'], 'completed')

    def test_failed_analysis_uses_official_react_tool_loop_and_records_observations(self):
        class ToolModels(Models):
            def __call__(owner, config):
                client = super().__call__(config)
                query = client.query
                def respond(messages, **kwargs):
                    if client.name == 'error_analyst' and len(messages) == 2:
                        client.n_calls += 1
                        owner.requests.append((client.name, copy.deepcopy(messages)))
                        return {'content': 'Action:\n{"name":"read_evidence","arguments":{"kind":"patch"}}'}
                    return query(messages, **kwargs)
                client.query = respond
                return client
        models = ToolModels()
        with patch.object(Backend, 'score', return_value={'success': False}):
            self.run_task(models)
        for route in range(5):
            path = self.output / f'example/trace2skill/rollout-{route}'
            events = read_json(path / 'tools.json')
            self.assertEqual(events[0]['tool'], 'read_evidence')
            self.assertIn(f'patch-rollout-{route}', events[0]['result']['text'])
            self.assertEqual(read_json(path / 'agent-result.json')['total_turns'], 2)
        self.assertEqual(len([m for role, m in models.requests if role == 'error_analyst']), 15)
        self.assertTrue((self.output / 'example/trace2skill/rollout-0/continuation-result.json').exists())

    def test_analysis_errors_are_archived_and_prevent_task_commit(self):
        class BrokenAnalysisModels(Models):
            def __call__(owner, config):
                client = super().__call__(config)
                if client.name == 'success_analyst':
                    def fail(*args, **kwargs):
                        raise RuntimeError('injected Max analysis failure')
                    client.query = fail
                return client
        with self.assertRaisesRegex(RuntimeError, 'analysis failed'):
            self.run_task(BrokenAnalysisModels())
        self.assertEqual(read_json(self.output / 'checkpoint.json')['completed'], [])
        self.assertEqual(read_json(self.output / 'example/update.json')['decision'], 'update_error')
        self.assertTrue(list((self.output / 'example/trace2skill').glob('rollout-*/analysis-error.json')))
        self.assertFalse((self.output / 'example/trace2skill/editor-records.json').exists())

    def test_fixed_skill_never_calls_analysis_or_evolution(self):
        config = test_config()
        config['evolve'] = False
        models = Models()
        self.run_task(models, config)
        self.assertEqual({role for role, _ in models.requests}, {'solver', 'judge'})
        self.assertFalse((self.output / 'example/trace2skill').exists())
        self.assertEqual(len(read_json(self.output / 'example/all-route-scores.json')['scores']), 1)

    def test_probe_is_isolated_applies_exact_patch_and_cleans_container(self):
        calls = []
        class FakeDocker:
            def __init__(self, **kwargs):
                self.container_id = 'analysis-container'
                self.config = SimpleNamespace(executable='docker')
                self.kwargs = kwargs
            def execute(self, command):
                calls.append(command)
                return {'returncode': 0, 'output': 'base' if command == 'git rev-parse HEAD' else 'probe output'}
        task = {'benchmark': 'swe', 'instance': {'instance_id': 'example', 'base_commit': 'base', 'image': 'cached-image'}}
        reader = EvidenceReader(task, {'rollout': 0, 'artifact': 'exact patch'}, {}, Path(self.temp.name) / 'trace2skill/rollout-0')
        with patch('rsi.benchmarks.backends.CompatibleDockerEnvironment', FakeDocker), \
             patch('rsi.skills.trace2skill.evidence.subprocess.run', return_value=SimpleNamespace(returncode=0, stderr='')) as remove:
            try:
                output = json.loads(reader.run_probe('print(1)'))
                self.assertIn('probe output', output['output'])
                self.assertEqual(reader.env.kwargs['run_args'][1:3], ['--network', 'none'])
                self.assertTrue(any('git apply' in command for command in calls))
            finally:
                reader.close()
            remove.assert_called_once()
        self.assertTrue(read_json(reader.folder / 'probe-cleanup.json')['removed'])

    def test_diagnostic_export_drops_probe_caches_and_uses_a_longer_timeout(self):
        seen = []
        class FakeDocker:
            def __init__(self, **kwargs):
                self.container_id = 'analysis-container'
                self.config = SimpleNamespace(executable='docker', timeout=30)
            def execute(self, command):
                seen.append((command, self.config.timeout))
                return {'returncode': 0, 'output': 'base' if command == 'git rev-parse HEAD' else 'diff --git a/src/click/core.py'}
        task = {'benchmark': 'swe', 'instance': {'instance_id': 'example', 'base_commit': 'base', 'image': 'cached-image'}}
        reader = EvidenceReader(task, {'rollout': 0, 'artifact': 'exact patch'}, {}, Path(self.temp.name) / 'trace2skill/rollout-0')
        with patch('rsi.benchmarks.backends.CompatibleDockerEnvironment', FakeDocker):
            reader.run_probe('print(1)')
            patch_text = reader.diagnostic_patch()
        self.assertEqual(patch_text, 'diff --git a/src/click/core.py')
        command, timeout = seen[-1]
        self.assertIn('__pycache__', command)
        self.assertIn('.pytest_cache', command)
        self.assertIn('git diff --cached --binary base', command)
        self.assertNotIn('git add -A &&', command)
        self.assertEqual(timeout, 120)
        self.assertEqual(reader.env.config.timeout, 30)

    def test_watchdog_observes_trace2skill_activity_and_errors(self):
        path = self.output / 'example/trace2skill/flash/call-0001.request.json'
        path.parent.mkdir(parents=True)
        path.write_text('{}')
        self.assertIn(path, activity_paths(self.output / 'example'))
        self.assertIn('Trace2Skill', problem_in(path.parent.parent / 'summary.json', {'status': 'failed'}))


class ReferencePlacementTests(unittest.TestCase):
    def test_editor_prompts_ask_for_a_reference_before_the_line_limit(self):
        from rsi.skills.trace2skill.pipeline import attach_reference_placement
        from rsi.skills.trace2skill.prompts import EDITOR_REFERENCE_PLACEMENT
        from rsi.skills.trace2skill.upstream import load
        _, _, _, _, _, Combined, _, _ = load()
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__('shutil').rmtree(root))
        (root / 'SKILL.md').write_text('---\nname: demo\ndescription: Demo workflow.\n---\n# Demo\n', encoding='utf-8')
        evolver = Combined(client=object(), skill_dir=root, parse_failure_dir=root / 'parse-failures', verbose=False)
        attach_reference_placement(evolver)
        instruction = EDITOR_REFERENCE_PLACEMENT.strip()
        for prompt in (evolver._map_system_prompt, evolver._map_patterns_system_prompt, evolver._merge_system_prompt):
            self.assertIn(instruction, prompt)
        self.assertIn('Propose MINIMAL, TARGETED edits', evolver._map_system_prompt)
        user = evolver._build_map_user_message({'SKILL.md': (root / 'SKILL.md').read_text(encoding='utf-8')}, [], 1, 1)
        self.assertIn('(limit: 500)', user)
        self.assertNotIn('approaching the line limit', user)
        self.assertTrue(user.rstrip().endswith(instruction))
        merged = evolver._build_merge_user_message({'SKILL.md': 'x'}, [])
        self.assertTrue(merged.rstrip().endswith(instruction))
        attach_reference_placement(evolver)
        self.assertEqual(evolver._map_system_prompt.count(instruction), 1)


if __name__ == '__main__':
    unittest.main()
