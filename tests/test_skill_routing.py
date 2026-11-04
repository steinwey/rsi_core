import copy
import json
import math
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from rsi.agent.execution import skill_prompt
from rsi.agent.runner import run_experiment, run_group
from rsi.core.common import digest, read_json
from rsi.core.config import load_config
from rsi.experiment import checkpoints
from rsi.skills.budget import BudgetError
from rsi.skills.directory import INDEX_PATH, directory_schema, read_skill_directory
from rsi.skills.index import build_index, sha, validate_index
from rsi.skills.routing import (exclusion_match, retrieve_bm25, route, settings, tokenize)
from rsi.skills.store import SkillStore, load_initial_skill
from routing_fixtures import CharacterCounter, files
import test_maintenance_transactions as p0
from test_core_baseline import Backend, Models, test_config


def ranked(query, source):
    return retrieve_bm25(query, source, validate_index(source))


class RetrievalTests(unittest.TestCase):
    def test_exact_keyword_prefers_related_reference(self):
        source = files({'references/pytest.md': 'pytest fixture', 'references/sql.md': 'SQL parser'})
        result = ranked('pytest fixture', source)
        self.assertEqual(result[0].path, 'references/pytest.md')
        self.assertGreater(result[0].score, 0)
        self.assertEqual(result[1].score, 0)

    def test_api_names_and_errors(self):
        source = files({'references/a.md': 'AttributeError get_absolute_url astropy.coordinates sympy.core'})
        for query in ('attributeerror', 'get_absolute_url', 'astropy.coordinates', 'sympy.core'):
            with self.subTest(query=query):
                self.assertGreater(ranked(query, source)[0].score, 0)

    def test_snake_case_full_and_components(self):
        self.assertEqual(tokenize('get_absolute_url'), ('get_absolute_url', 'get', 'absolute', 'url'))
        self.assertGreater(ranked('absolute', files({'references/a.md': 'get_absolute_url'}))[0].score, 0)

    def test_camel_case_acronym_and_error_components(self):
        self.assertEqual(tokenize('AttributeError'), ('attributeerror', 'attribute', 'error'))
        self.assertEqual(tokenize('HTTPServer'), ('httpserver', 'http', 'server'))

    def test_dotted_module_preserved_and_split(self):
        self.assertEqual(tokenize('astropy.coordinates'), ('astropy.coordinates', 'astropy', 'coordinates'))
        self.assertEqual(tokenize('sympy.core'), ('sympy.core', 'sympy', 'core'))

    def test_case_normalization_and_fixed_stopwords(self):
        self.assertEqual(tokenize('The PYTEST fixture and pytest Fixture'), ('pytest', 'fixture', 'pytest', 'fixture'))
        self.assertEqual(tokenize('the and of'), ())

    def test_cjk_bigram(self):
        self.assertEqual(tokenize('中文错误'), ('中文', '文错', '错误'))
        self.assertEqual(tokenize('错'), ('错',))

    def test_zero_match_does_not_fill_top_k(self):
        self.assertTrue(all(c.score == 0 for c in ranked('unrelatedxyz', files({'references/a.md': 'pytest'}))))

    def test_tied_scores_sort_by_path_independent_of_input_order(self):
        source = files({'references/z.md': 'pytest', 'references/a.md': 'pytest'})
        index = validate_index(source)
        index['entries'].reverse()
        self.assertEqual(retrieve_bm25('pytest', source, index), ranked('pytest', source))
        self.assertEqual([r.path for r in ranked('pytest', source)], ['references/a.md', 'references/z.md'])

    def test_standard_bm25_single_document_formula(self):
        source = files({'references/a.md': 'pytest pytest'})
        # TF=2, N=df=1 and length/average=1.
        expected = math.log(1 + .5 / 1.5) * 2 * 2.2 / (2 + 1.2)
        self.assertAlmostEqual(ranked('pytest pytest', source)[0].score, expected)

    def test_equal_weight_scoring_fields(self):
        for field, value in [('title', 'fixture'), ('description', 'fixture'), ('keywords', ['fixture']),
                             ('applies_when', ['fixture'])]:
            source = files({'references/a.md': 'unrelated'}, metadata={'references/a.md': {field: value}})
            with self.subTest(field=field):
                self.assertGreater(ranked('fixture', source)[0].score, 0)

    def test_exclusions_are_phrase_filters_not_positive_evidence(self):
        source = files({'references/a.md': 'pytest'}, metadata={'references/a.md': {'exclude_when': ['SQL parser']}})
        candidate = ranked('pytest SQL parser', source)[0]
        self.assertEqual(candidate.exclusion, 'SQL parser')
        self.assertEqual(ranked('SQL parser', source)[0].score, 0)
        self.assertIsNone(exclusion_match('not about sql parsers', ['SQL parser']))
        self.assertIsNone(exclusion_match('pytest is relevant', ['not pytest']))

    def test_empty_corpus(self):
        self.assertEqual(ranked('pytest', files()), ())


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = {'skill_directory': {'schema': 1}, 'skill_routing': {'enabled': True},
                       'model': {'name': 'mock-solver'}}
        self.task = {'id': 'mock-task', 'prompt': 'pytest fixture'}
        self.source = files({'references/pytest.md': '# pytest\nfixture\n', 'references/sql.md': 'SQL only'})
        self.skill = self.snapshot(self.source)

    def snapshot(self, source):
        location = self.root / digest(source); location.mkdir(exist_ok=True)
        for name, text in source.items():
            target = location / name; target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(text.encode())
        return location

    def run_route(self, **kwargs):
        return route(self.task, self.skill, self.config, counter_factory=lambda cfg: CharacterCounter(cfg['name']), **kwargs)

    def test_reproducible_identity_and_no_snapshot_mutation(self):
        first, second = self.run_route(), self.run_route()
        self.assertEqual(first.context, second.context)
        a, b = first.record(), second.record(); a.pop('seconds'); b.pop('seconds')
        self.assertEqual(a, b)
        self.assertEqual(read_skill_directory(self.skill, directory_schema=1), self.source)
        self.assertEqual(a['index_hash'], sha(self.source[INDEX_PATH]))
        self.assertEqual(a['skill_version'], digest(self.source))

    def test_hidden_fields_never_used(self):
        original = self.run_route()
        self.task.update(golden_patch='SQL only', success=False, feedback='SQL', test_labels=['SQL'])
        self.assertEqual(original.context, self.run_route().context)

    def test_zero_and_filtered_matches_core_only(self):
        self.task['prompt'] = 'nohitsxyz'
        self.assertEqual(self.run_route().record()['outcome'], 'no_relevant_references')
        source = files({'references/a.md': 'pytest'}, metadata={'references/a.md': {'exclude_when': ['fixture']}})
        self.skill = self.snapshot(source); self.task['prompt'] = 'pytest fixture'
        result = self.run_route().record()
        self.assertEqual(result['selected_references'], [])
        self.assertEqual(result['excluded_references'][0]['reason'], 'exclude_when')

    def test_all_candidates_over_budget_logged(self):
        self.skill = self.snapshot(files({'references/a.md': 'pytest ' * 1500}))
        result = self.run_route().record()
        self.assertEqual(result['outcome'], 'references_over_budget')
        self.assertEqual(result['excluded_references'][0]['reason'], 'reference_over_budget')

    def test_no_match_disallowed_is_explicit(self):
        self.task['prompt'] = 'xyznohit'; self.config['skill_routing']['allow_zero_matches'] = False
        with self.assertRaises(BudgetError) as caught: self.run_route(folder=self.root)
        self.assertEqual(caught.exception.code, 'zero_matches_disallowed')

    def test_core_failure_logged(self):
        self.skill = self.snapshot(files(root='core' * 1500))
        with self.assertRaises(BudgetError): self.run_route(folder=self.root)
        self.assertEqual(read_json(self.root / 'skill-route.json')['reason'], 'core_over_budget')
        self.assertFalse((self.root / 'skill-context.txt').exists())

    def test_counter_unavailable_logged_without_api(self):
        self.config['model']['name'] = 'gpt-6-luna'
        with self.assertRaises(BudgetError): route(self.task, self.skill, self.config, folder=self.root)
        log = read_json(self.root / 'skill-route.json')
        self.assertEqual(log['reason'], 'counter_unavailable')
        self.assertEqual(log['counter']['accuracy'], 'unsupported')
        self.assertIsNone(log['initial_skill_tokens'])

    def test_index_corruption_not_disguised_as_no_match(self):
        (self.skill / 'references/pytest.md').write_text('tampered', encoding='utf-8')
        with self.assertRaises(BudgetError): self.run_route(folder=self.root)
        self.assertEqual(read_json(self.root / 'skill-route.json')['reason'], 'index_invalid')

    def test_version_mismatch_rejected(self):
        (self.skill / 'SKILL.md').write_text('different core', encoding='utf-8')
        with self.assertRaises(BudgetError) as caught: self.run_route(folder=self.root)
        self.assertEqual(caught.exception.code, 'skill_version_mismatch')

    def test_full_context_archived_hash_matches(self):
        result = self.run_route(folder=self.root)
        archived = (self.root / 'skill-context.txt').read_bytes().decode('utf-8')
        self.assertEqual(archived, result.context.text)
        self.assertEqual(sha(archived), result.record()['context_hash'])
        self.assertNotIn('SQL only', archived)
        self.assertTrue((self.skill / 'references/sql.md').exists())

    def test_route_changes_with_prompt_budget_and_index(self):
        original = self.run_route().context.route_id
        self.task['prompt'] += ' pytest'
        self.assertNotEqual(original, self.run_route().context.route_id)
        self.task['prompt'] = 'pytest fixture'; self.config['skill_routing']['top_k'] = 1
        self.assertNotEqual(original, self.run_route().context.route_id)
        self.config['skill_routing']['top_k'] = 2
        changed = dict(self.source); changed[INDEX_PATH] += '# metadata comment\n'
        self.skill = self.snapshot(changed)
        self.assertNotEqual(original, self.run_route().context.route_id)

    def test_returned_record_cannot_mutate_frozen_route(self):
        result = self.run_route(); record = result.record(); record['selected_references'].clear()
        self.assertEqual(result.context.selected_references, ('references/pytest.md',))
        self.assertEqual(result.record()['selected_references'], ['references/pytest.md'])


class RoutingConfigTests(unittest.TestCase):
    def test_defaults_and_no_input_mutation(self):
        config = {}; result = settings(config)
        self.assertFalse(result['enabled']); self.assertEqual(result['top_k'], 2)
        self.assertEqual(result['max_initial_skill_tokens'], 4000)
        self.assertEqual(result['bm25'], {'k1': 1.2, 'b': .75})
        result['bm25']['k1'] = 9
        self.assertEqual(settings({})['bm25']['k1'], 1.2)
        self.assertEqual(config, {})

    def test_invalid_parameters(self):
        cases = [('top_k', 0), ('top_k', -1), ('top_k', True), ('top_k', 2.5), ('top_k', 1001),
                 ('max_initial_skill_tokens', 0), ('max_initial_skill_tokens', True),
                 ('retrieval_method', 'jev'), ('budget_mode', 'estimated'), ('enabled', 1),
                 ('allow_zero_matches', 'yes'), ('unknown', 2), ('bm25', []),
                 ('bm25', {'k1': 0}), ('bm25', {'k1': float('nan')}), ('bm25', {'k1': True}),
                 ('bm25', {'b': 1.1}), ('bm25', {'b': -0.1}), ('bm25', {'b': float('inf')}),
                 ('bm25', {'unknown': 2})]
        for key, value in cases:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                settings({'skill_routing': {key: value}})

    def test_schema_and_framework_compatibility(self):
        with self.assertRaisesRegex(ValueError, 'P0'):
            directory_schema({'skill_routing': {'enabled': True}})
        with self.assertRaisesRegex(ValueError, 'Trace2Skill'):
            directory_schema({'skill_directory': {'schema': 1}, 'skill_routing': {'enabled': True},
                              'skill_update_method': 'reasoningbank'})
        self.assertEqual(directory_schema({'skill_directory': {'schema': 1}, 'skill_routing': {'enabled': True}}), 1)

    def test_loader_persists_expanded_parameters(self):
        from rsi import ROOT
        source = yaml.safe_load((ROOT / 'configs/agent/qwen.yaml').read_text())
        source.update(skill_directory={'schema': 1}, skill_routing={'enabled': True})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.yaml'; path.write_text(yaml.safe_dump(source), encoding='utf-8')
            cfg = load_config(path)
        self.assertEqual(cfg['skill_routing'], settings(cfg))


class RoutingIntegrationTests(unittest.TestCase):
    def setUp(self):
        p0.ManagedCheckpointTests.setUp(self)
        self.config['skill_routing'] = settings({'skill_routing': {'enabled': True, 'max_initial_skill_tokens': 20000}})
        self.models = Models(no_changes=True)
        source, _ = load_initial_skill(directory_schema=1, bootstrap=True)
        source.update({'references/pytest.md': '# pytest\nfixture visible issue\n',
                       'references/other.md': 'UNSELECTED_BODY_SENTINEL quantum cosmology'})
        source[INDEX_PATH] = build_index(source)
        self.seed = SkillStore(Path(self.tmp.name) / 'seed', source, directory_schema=1).current()
        self.config['initial_skill'] = str(self.seed)
        self.tasks[0]['prompt'] = 'pytest fixture'
        self.counter_calls = []

    def counter_factory(self, config):
        self.counter_calls.append(config['name']); return CharacterCounter(config['name'])

    def run_tasks(self, resume=False):
        return run_experiment(self.tasks, self.config, self.output, resume=resume,
                              backend_factory=Backend, model_factory=self.models, counter_factory=self.counter_factory)

    def test_once_before_pool_five_identical_actual_contexts(self):
        from rsi.skills.routing import route as real_route
        from rsi.agent.execution import execute_one as real_execute
        from concurrent.futures import ThreadPoolExecutor
        contexts = []
        def execute(*args, **kwargs):
            contexts.append(kwargs['skill_context'])
            return real_execute(*args, **kwargs)
        def pool(*args, **kwargs):
            self.assertEqual(router.call_count, 1)
            self.assertTrue((self.output / 'task/skill-context.txt').exists())
            return ThreadPoolExecutor(*args, **kwargs)
        with patch('rsi.agent.runner.route_skill', wraps=real_route) as router, \
                patch('rsi.agent.runner.ThreadPoolExecutor', side_effect=pool), \
                patch('rsi.agent.runner.execute_one', side_effect=execute):
            reports = self.run_tasks()
        self.assertEqual(len(contexts), 5)
        self.assertTrue(all(c is contexts[0] for c in contexts))
        self.assertEqual(router.call_count, 1); self.assertEqual(self.counter_calls, ['solver'])
        self.assertEqual(set(router.call_args.args[0]), {'id', 'prompt'})
        record = read_json(self.output / 'task/skill-route.json')
        context = (self.output / 'task/skill-context.txt').read_text(encoding='utf-8')
        actual = []
        for i in range(5):
            trajectory = read_json(self.output / f'task/rollout-{i}/trajectory.json')
            for key in ('route_id', 'context_hash', 'index_hash', 'skill_version'):
                self.assertEqual(trajectory[key], record[key])
            system = next(m['content'] for m in trajectory['messages'] if m['role'] == 'system')
            self.assertTrue(system.endswith(context)); actual.append(system)
            self.assertNotIn('UNSELECTED_BODY_SENTINEL', system)
            self.assertEqual(trajectory['artifact'], f'patch-rollout-{i}')
        self.assertEqual(len(set(actual)), 1)
        self.assertEqual(reports[0]['graded_rollouts'], [0, 1, 2, 3, 4])
        self.assertEqual(reports[0]['selector_usage']['calls'], 0)
        self.assertTrue(all('references/other.md' in b.env.skill and INDEX_PATH in b.env.skill for b in Backend.instances))
        self.assertEqual(read_json(self.output / 'checkpoint.json')['schema'], 6)

    def test_selector_and_selected_only_scoring_unchanged(self):
        self.config['evolve'] = False
        reports = self.run_tasks()
        self.assertEqual(reports[0]['selected'], 4)
        self.assertEqual(reports[0]['graded_rollouts'], [4])
        self.assertTrue(reports[0]['success'])
        self.assertEqual(reports[0]['selector_usage']['calls'], 1)

    def test_single_route_frozen_test_same_schema(self):
        self.config.update(evolve=False, rollouts=1, rollout_workers=1)
        self.config['execution_mode'] = 'single'
        Backend.barrier = threading.Barrier(1)
        report = self.run_tasks()[0]
        self.assertEqual(report['graded_rollouts'], [0])
        self.assertEqual(read_json(self.output / 'task/skill-route.json')['schema'], 1)

    def test_disabled_exact_original_guide_no_router_or_counter(self):
        self.config['skill_routing'] = {'enabled': False}
        with patch('rsi.agent.runner.route_skill', side_effect=AssertionError('must not route')):
            self.run_tasks()
        self.assertEqual(self.counter_calls, [])
        self.assertFalse((self.output / 'task/skill-route.json').exists())
        trajectory = read_json(self.output / 'task/rollout-0/trajectory.json')
        self.assertNotIn('route_id', trajectory)
        system = next(m['content'] for m in trajectory['messages'] if m['role'] == 'system')
        self.assertTrue(system.endswith(skill_prompt(self.seed)))

    def test_fail_closed_before_any_solver_or_backend(self):
        self.config['skill_routing']['max_initial_skill_tokens'] = 1
        with self.assertRaises(BudgetError): self.run_tasks()
        self.assertEqual(Backend.instances, []); self.assertEqual(self.models.requests, [])
        self.assertEqual(read_json(self.output / 'checkpoint.json')['completed'], [])
        self.assertEqual(read_json(self.output / 'task/skill-route.json')['reason'], 'core_over_budget')

    def test_after_commit_failure_resume_does_not_route_or_call_models(self):
        with patch('rsi.experiment.checkpoints.publish', side_effect=OSError('publish failed')), self.assertRaises(OSError):
            self.run_tasks()
        self.models.requests.clear(); self.counter_calls.clear()
        with patch('rsi.agent.runner.route_skill', side_effect=AssertionError('rerouted committed task')):
            self.run_tasks(resume=True); self.run_tasks(resume=True)
        self.assertEqual(self.models.requests, []); self.assertEqual(self.counter_calls, [])
        self.assertEqual(len(read_json(self.output / 'checkpoint.json')['completed']), 1)

    def test_precommit_failure_archives_and_reproduces_context(self):
        with patch('rsi.agent.runner.commit', side_effect=OSError('before commit')), self.assertRaises(OSError):
            self.run_tasks()
        first = read_json(self.output / 'task/skill-route.json')
        self.run_tasks(resume=True)
        second = read_json(self.output / 'task/skill-route.json')
        self.assertEqual(first['route_id'], second['route_id'])
        self.assertEqual(len(list((self.output / 'attempts').iterdir())), 1)

    def test_changed_routing_config_rejected_on_resume(self):
        self.run_tasks(); self.config['skill_routing']['top_k'] = 1
        with self.assertRaisesRegex(ValueError, 'without overrides'): self.run_tasks(resume=True)

    def test_context_tampering_rejects_resume(self):
        self.run_tasks()
        (self.output / 'task/skill-context.txt').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'Routing artifact'): checkpoints.resume_request(self.output)

    def test_route_log_tampering_rejects_resume(self):
        self.run_tasks()
        with (self.output / 'task/skill-route.json').open('ab') as stream: stream.write(b' ')
        with self.assertRaisesRegex(ValueError, 'Routing receipt'): checkpoints.resume_request(self.output)

    def test_next_task_routes_new_committed_skill_once(self):
        self.models = Models()
        self.tasks.append({'id': 'second', 'benchmark': 'swe', 'prompt': 'pytest fixture'})
        self.run_tasks()
        first = read_json(self.output / 'task/skill-route.json')
        second = read_json(self.output / 'second/skill-route.json')
        update = read_json(self.output / 'task/update.json')
        self.assertTrue(update['accepted'])
        self.assertEqual(second['skill_version'], update['after_skill_version'])
        self.assertNotEqual(first['skill_version'], second['skill_version'])
        self.assertEqual(self.counter_calls, ['solver', 'solver'])
        self.assertEqual(second['selected_references'], first['selected_references'])

    def test_combined_p1_p2_preserves_maintenance(self):
        self.config['skill_maintenance'] = {'enabled': True}
        self.models = Models()
        self.run_tasks()
        receipt = read_json(self.output / 'task/skill-route.json')
        update = read_json(self.output / 'task/update.json')
        self.assertTrue(update['accepted'])
        self.assertEqual(update['maintenance']['status'], 'accepted')
        self.assertEqual(receipt['skill_version'], update['before_skill_version'])
        checkpoints.resume_request(self.output)

    def test_unsupported_default_counter_starts_no_workers(self):
        with patch('rsi.agent.runner.ThreadPoolExecutor', side_effect=AssertionError('must not start')):
            with self.assertRaises(BudgetError):
                run_experiment(self.tasks, self.config, self.output, backend_factory=Backend, model_factory=self.models)
        self.assertEqual(Backend.instances, [])
        self.assertEqual(self.models.requests, [])
        self.assertEqual(read_json(self.output / 'task/skill-route.json')['reason'], 'counter_unavailable')

    def test_missing_context_in_execution_does_not_reroute(self):
        from rsi.agent.execution import execute_one
        with patch('rsi.skills.routing.retrieve_bm25', side_effect=AssertionError('must not retrieve')):
            result, backend = execute_one(self.tasks[0], 0, Path(self.tmp.name) / 'direct', self.seed,
                                          self.config, Backend, self.models)
        self.assertIn('Missing or inconsistent frozen SkillContext', result['error'])
        self.assertIsNone(backend)
        self.assertEqual(Backend.instances, [])

    def test_changed_context_rejected_before_install(self):
        from dataclasses import replace
        from rsi.agent.execution import execute_one
        context = route(self.tasks[0], self.seed, self.config, counter_factory=self.counter_factory).context
        context = replace(context, text=context.text + 'tampered')
        result, _ = execute_one(self.tasks[0], 0, Path(self.tmp.name) / 'direct', self.seed,
                                self.config, Backend, self.models, skill_context=context)
        self.assertIn('inconsistent frozen SkillContext', result['error'])
        self.assertEqual(Backend.instances, [])
