import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rsi.core.common import read_json, write_json
from rsi.experiment import checkpoints
from rsi.skills import structural
from rsi.skills.directory import read_skill_directory
from rsi.skills.index import validate_index
from rsi.skills.maintenance import maintain_candidate, settings
from rsi.skills.markdown_units import canonical, parse_experience_units
from rsi.skills.store import load_initial_skill, SkillStore
from test_skill_migration import PlannerModels, plan_for
import test_maintenance_transactions as p0

FRONT = '''---
name: test-and-diagnose
description: >-
  Diagnose and fix SWE-bench issues in software repositories. Use when
  reproducing the reported bug and selecting targeted regression tests.
---

'''
GENERAL = ['Inspect the repository and reproduce the reported failure before changing production code.',
           'Prefer the smallest relevant test, then related regression tests.',
           'Report observed failures and distinguish environment errors from product bugs.']


def click_item(n, size=0):
    text = (f'{n}. When `click.Option` receives value {n}, call `ctx.ensure_object(dict)` before reading '
            f'`ctx.params`; keep the `UsageError` message stable.')
    return text + ' Check the parser callback.' * size + '\n'


def is_click(unit):
    return 'click.Option' in unit['text']


def click_plan(data):
    return plan_for(data, 'references/click.md', select=is_click, limit=64)


def mode_of(messages):
    return json.loads(messages[1]['content']).get('mode')


class StructuralMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base, _ = load_initial_skill(directory_schema=1, bootstrap=True)
        self.config = {'skill_directory': {'schema': 1}, 'skill_update_method': 'trace2skill',
                       'skill_maintenance': settings({'skill_maintenance': {'enabled': True,
                                                                            'min_new_reference_chars': 0}}),
                       'skill_editor_model': {'name': 'mock', 'num_retries': 2}}
        self.options = structural.options(self.config)
        self.models = PlannerModels(planner=click_plan)

    def request(self, **state):
        return structural.request(structural.initial_state() | state, self.options)

    def maintain(self, text, folder='maintenance', **state):
        result = maintain_candidate(self.base, {'SKILL.md': text}, self.config, self.root / folder,
                                    model_factory=self.models, structural=self.request(**state))
        return result, read_json(self.root / folder / 'validation.json')

    def plans(self):
        return [m for m in self.models.requests if m[0]['content'].startswith('Propose')]

    def test_settings_default_interval_and_legacy_configs_stay_inactive(self):
        self.assertEqual(self.options, {'interval': 5})
        self.assertIsNone(structural.options({'skill_maintenance': {'enabled': True}}))
        self.assertIsNone(structural.options({'skill_maintenance': settings({'skill_maintenance': {
            'enabled': True, 'structural_migration': False}})}))
        for value in (0, 1001, True, 2.0):
            with self.subTest(value=value), self.assertRaises(ValueError):
                settings({'skill_maintenance': {'structural_review_interval': value}})

    def test_click_like_single_h1_numbered_skill_migrates_under_mandatory(self):
        body = '# Test and diagnose\n\nUse for SWE-bench software-repair tasks.\n\n'
        body += ''.join(f'{i}. {text}\n' for i, text in enumerate(GENERAL))
        body += ''.join(click_item(i, 12) for i in range(3, 27))
        source = FRONT + body
        self.assertGreater(len(source), 10000)
        result, receipt = self.maintain(source, effective_updates=4)
        files = result['files']
        self.assertLessEqual(len(files['SKILL.md']), 10000)
        self.assertEqual(receipt['structural']['mode'], 'mandatory')
        self.assertTrue(receipt['structural']['due_full'])
        self.assertEqual({mode_of(m) for m in self.plans()}, {'mandatory'})
        self.assertFalse(list((self.root / 'maintenance').glob('structural-*')))
        for text in GENERAL:
            self.assertIn(text, files['SKILL.md'])
        moved = [u.text.strip() for u in parse_experience_units(source) if 'click.Option' in u.text]
        references = '\n'.join(v for p, v in files.items() if p.startswith('references/'))
        remaining = [t for t in moved if t not in files['SKILL.md']]
        self.assertTrue(remaining)
        for text in remaining:
            self.assertIn(text, references)
        self.assertIn('\n3. Specific guidance (the task involves click): [Click](references/click.md)\n', files['SKILL.md'])
        numbers = [line.split('.')[0] for line in files['SKILL.md'].splitlines() if line[:1].isdigit()]
        self.assertEqual(numbers, [str(i) for i in range(27)])
        self.assertTrue(all(len(v) <= 5000 for p, v in files.items() if p.startswith('references/')))
        self.assertTrue(all(e['title'] == 'Click' for e in validate_index(files)['entries']))

    def test_structural_migration_runs_on_a_short_root_without_length_trigger(self):
        source = self.base['SKILL.md'] + '\n## Field notes\n\n' + ''.join(click_item(i, 10) for i in range(1, 4))
        self.assertLess(len(source), 3500)
        result, receipt = self.maintain(source)
        info = receipt['structural']
        self.assertEqual((info['mode'], info['status'], info['llm_called']), ('light', 'migrated', True))
        self.assertEqual(receipt['status'], 'accepted')
        self.assertLess(len(result['files']['SKILL.md']), len(source))
        self.assertIn('references/click.md', result['changes'])
        data = json.loads(self.plans()[0][1]['content'])
        self.assertEqual(data['mode'], 'light')
        self.assertEqual(len(data['focus']), 3)
        self.assertNotIn('root_chars', data)
        self.assertNotIn('root_target_chars', data)
        self.assertTrue((self.root / 'maintenance/structural-01-plan.request.json').exists())

    def test_light_check_without_signals_makes_no_model_call(self):
        source = self.base['SKILL.md'] + '\nKeep the final summary short and say what changed.\n'
        result, receipt = self.maintain(source)
        self.assertEqual(self.models.requests, [])
        self.assertEqual((receipt['structural']['mode'], receipt['structural']['status']), ('none', 'skipped'))
        self.assertEqual(result['files']['SKILL.md'], canonical(source))
        self.assertEqual(receipt['structural']['light_check']['changed_units'], 1)

    def test_fifth_update_full_review_without_keywords_or_length(self):
        source = self.base['SKILL.md'] + '\nKeep the final summary short and say what changed.\n'
        result, receipt = self.maintain(source, effective_updates=4)
        info = receipt['structural']
        self.assertEqual((info['mode'], info['status']), ('full', 'unchanged'))
        self.assertEqual(len(self.models.requests), 1)
        self.assertEqual(mode_of(self.models.requests[0]), 'full')
        units = {u.fingerprint for u in parse_experience_units(result['files']['SKILL.md']) if not u.protected}
        self.assertEqual(set(info['reviewed_after']), units)

    def test_light_trigger_on_fifth_update_runs_one_full_review(self):
        source = self.base['SKILL.md'] + '\n' + ''.join(click_item(i) for i in range(1, 3))
        _, receipt = self.maintain(source, effective_updates=9)
        self.assertEqual(receipt['structural']['mode'], 'full')
        self.assertTrue(receipt['structural']['light_check']['triggered'])
        self.assertEqual([mode_of(m) for m in self.plans()], ['full'])
        self.assertEqual(len(self.models.requests), 2)

    def test_structural_failure_keeps_valid_learning_candidate(self):
        source = self.base['SKILL.md'] + '\n' + ''.join(click_item(i) for i in range(1, 3))
        def bad_topic(data):
            plan = click_plan(data)
            plan['input_hash'] = 'stale'
            return plan
        def outage(data):
            raise ConnectionError('service unavailable')
        for name, planner in (('json', lambda data: '{'), ('plan', bad_topic), ('api', outage)):
            with self.subTest(name=name):
                self.models = PlannerModels(planner=planner)
                result, receipt = self.maintain(source, folder=name)
                info = receipt['structural']
                self.assertEqual(receipt['status'], 'accepted')
                self.assertEqual(info['status'], 'failed')
                self.assertEqual(result['files']['SKILL.md'], canonical(source))
                self.assertEqual(receipt['candidate_hash'], __import__('rsi.core.common', fromlist=['digest']).digest(result['files']))
                self.assertEqual(info['pending_after'], info['light_check']['candidates'])
                self.assertEqual(len(info['pending_after']), 2)

    def test_pending_candidates_are_retried_once(self):
        source = self.base['SKILL.md'] + '\n' + ''.join(click_item(i) for i in range(1, 3))
        self.models = PlannerModels(planner=lambda data: '{')
        _, first = self.maintain(source, folder='first')
        pending = first['structural']['pending_after']
        self.base = self.base | {'SKILL.md': canonical(source)}
        self.base['routing/index.yaml'] = __import__('rsi.skills.index', fromlist=['build_index']).build_index(self.base)
        later = canonical(source) + '\nKeep the final summary short.\n'
        _, second = self.maintain(later, folder='second', pending=pending)
        self.assertEqual(second['structural']['mode'], 'light')
        self.assertEqual(second['structural']['light_check']['pending'], pending)
        self.assertEqual(second['structural']['pending_after'], [])
        self.models = PlannerModels(planner=click_plan)
        _, third = self.maintain(later, folder='third', pending=pending)
        self.assertEqual(third['structural']['status'], 'migrated')

    def test_reviewed_units_are_not_rechecked_by_light_check(self):
        candidate = self.base | {'SKILL.md': self.base['SKILL.md'] + '\n' + click_item(1)}
        candidate['routing/index.yaml'] = self.base['routing/index.yaml']
        fresh = structural.light_check(self.base, candidate, structural.initial_state())
        self.assertTrue(fresh['triggered'])
        reviewed = structural.light_check(self.base, candidate, structural.initial_state() | {'reviewed': fresh['candidates']})
        self.assertFalse(reviewed['triggered'])

    def test_identifier_signals_are_shape_based(self):
        found = structural.identifiers('Use `pytest.fixture` with sqlfluff.core.Linter, parse_args(), '
                                       'camelCase, LintResult and a TemplateError.')
        for value in ('pytest.fixture', 'sqlfluff.core.linter', 'parse_args', 'camelcase', 'lintresult', 'templateerror'):
            self.assertIn(value, found)
        self.assertEqual(structural.identifiers('Prefer small focused tests and report results.'), [])

    def test_advance_verifies_request_and_bounds(self):
        state = structural.initial_state()
        receipt = {'mode': 'light', 'request_sha256': structural.request_sha256(structural.request(state, self.options)),
                   'pending_after': [], 'reviewed_after': ['a' * 64]}
        entry = structural.advance(state, receipt, True, self.options)
        self.assertEqual(entry['after'] | {}, state | {'effective_updates': 1, 'light_reviews': 1, 'reviewed': ['a' * 64]})
        self.assertEqual(structural.advance(state, None, False, self.options)['after'], state)
        for broken in ({'request_sha256': 'x'}, {'reviewed_after': ['b' * 64, 'a' * 64]}, {'pending_after': ['short']}):
            with self.subTest(broken=broken), self.assertRaises(ValueError):
                structural.advance(state, receipt | broken, True, self.options)


class StructuralCheckpointTests(unittest.TestCase):
    """Migration and P3 consolidation share one candidate transaction and commit point."""

    def setUp(self):
        p0.ManagedCheckpointTests.setUp(self)
        from test_reference_consolidation import sample, plan_for as consolidation_plan
        self.config.update(skill_maintenance={'enabled': True, 'structural_migration': True,
                                              'min_new_reference_chars': 0},
                           reference_maintenance={'enabled': True})
        self.tasks = [{'id': f'task-{i:02d}', 'benchmark': 'swe', 'prompt': 'Fix visible issue'} for i in range(1, 11)]
        self.seed = sample()
        self.source = SkillStore(Path(self.tmp.name) / 'source', self.seed, directory_schema=1).current()
        self.config['initial_skill'] = str(self.source)
        def migrate(data):
            return plan_for(data, 'references/callers.md', limit=64, select=lambda u: 'Verify task' in u['text'])
        def consolidate(data):
            seed = next(read_json(p)['files'] for p in self.output.glob('task-*/consolidation/savepoint.json')
                        if read_json(p)['version'] == data['input_hash'])
            return consolidation_plan(seed, 'KEEP_SEPARATE')
        self.migration, self.consolidation = PlannerModels(planner=migrate), PlannerModels(planner=consolidate)
        def factory(options):
            basic, moving, merging = self.models(options), self.migration(options), self.consolidation(options)
            query, stats = basic.query, basic.stats
            def dispatch(messages, **kwargs):
                prompt = messages[0]['content']
                if prompt.startswith(('Propose a lossless', 'Independently audit')):
                    return moving.query(messages, **kwargs)
                if prompt.startswith(('Propose ONE bounded', 'Independently review this lossless')):
                    return merging.query(messages, **kwargs)
                return query(messages, **kwargs)
            basic.query = dispatch
            basic.stats = lambda: {k: stats()[k] + moving.stats()[k] + merging.stats()[k] for k in stats()}
            return basic
        self.factory = factory
        def learn(task, results, skill, *args, **kwargs):
            text = (skill / 'SKILL.md').read_text(encoding='utf-8')
            return {'changes': {'SKILL.md': text + '\nVerify task ' + task['id'] + ' callers.\n'},
                    'reason': 'Mock effective learning', 'evidence_rollouts': list(range(5))}
        mock = patch('rsi.agent.runner.evolve_skill', side_effect=learn)
        mock.start()
        self.addCleanup(mock.stop)

    def run_tasks(self, resume=False):
        from rsi.agent.runner import run_experiment
        from test_core_baseline import Backend
        return run_experiment(self.tasks, self.config, self.output, resume=resume,
                              backend_factory=Backend, model_factory=self.factory)

    def test_fifth_and_tenth_full_reviews_then_consolidation_on_migrated_candidate(self):
        self.run_tasks()
        state = read_json(self.output / 'checkpoint.json')
        self.assertEqual(state['structural_migration']['effective_updates'], 10)
        self.assertEqual(state['structural_migration']['full_reviews'], 2)
        modes = [e['update']['structural_migration']['mode'] for e in state['completed']]
        self.assertEqual(modes, ['none'] * 4 + ['full'] + ['none'] * 4 + ['full'])
        plans = [m for m in self.migration.requests if m[0]['content'].startswith('Propose')]
        self.assertEqual(len(plans), 2)
        tenth = state['completed'][-1]['update']
        self.assertTrue(tenth['reference_maintenance']['due'])
        self.assertEqual(tenth['maintenance']['candidate_hash'], tenth['learning_skill_version'])
        self.assertEqual(tenth['consolidation']['savepoint_version'], tenth['learning_skill_version'])
        savepoint = read_skill_directory(self.output / 'skills/versions' / tenth['learning_skill_version'], directory_schema=1)
        self.assertNotIn('Verify task task-10 callers.', savepoint['SKILL.md'])
        self.assertIn('Verify task task-10 callers.', savepoint['references/callers.md'])
        self.assertIn('Verify task task-05 callers.', savepoint['references/callers.md'])
        self.assertEqual(read_json(self.output / 'skills/current.json')['version'], tenth['after_skill_version'])
        before = len(self.migration.requests), len(self.consolidation.requests)
        self.run_tasks(resume=True)
        self.assertEqual(before, (len(self.migration.requests), len(self.consolidation.requests)))

    def test_resume_rejects_tampered_structural_state_or_entry(self):
        self.tasks = self.tasks[:5]
        self.run_tasks()
        path = self.output / 'checkpoint.json'
        original = read_json(path)
        state = json.loads(json.dumps(original))
        state['structural_migration']['effective_updates'] = 4
        write_json(path, state)
        with self.assertRaisesRegex(ValueError, 'Structural migration checkpoint state mismatch'):
            checkpoints.resume_request(self.output)
        state = json.loads(json.dumps(original))
        state['completed'][2]['update']['structural_migration']['mode'] = 'full'
        write_json(path, state)
        with self.assertRaisesRegex(ValueError, 'Structural migration schedule mismatch'):
            checkpoints.resume_request(self.output)
        write_json(path, original)
        checkpoints.resume_request(self.output)


if __name__ == '__main__':
    unittest.main()
