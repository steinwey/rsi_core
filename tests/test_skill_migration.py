import copy
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rsi.core.common import digest, read_json
from rsi.skills.directory import INDEX_PATH, read_skill_directory, directory_schema
from rsi.skills.index import build_index, validate_index
from rsi.skills.maintenance import maintain_candidate, MaintenanceRejected, settings, validate_limits
from rsi.skills.markdown_units import (parse_units, parse_experience_units, canonical, sha, UnsafeMarkdown,
                                       rebase_links, CONDITION)
from rsi.skills.migration import apply_plan, validate_coverage, split_references
from rsi.skills.store import load_initial_skill, SkillStore, SkillProposalRejected, document_gate


def section(title='Click options', size=3200):
    prefix = f'## {title}\n\nWhen `click.Option` receives multiple values, preserve the caller contract.\n'
    suffix = '\nUnless the value is missing, retain its shape. Verify the regression test.\n'
    return prefix + 'Check inputs and outputs. ' * ((size - len(prefix) - len(suffix)) // 26) + suffix


def scoped(unit):
    return CONDITION.match(unit['text']) is not None and '`' in unit['text']


def plan_for(data, target='references/click.md', *, select=None, limit=1, topic=None):
    """Deterministic fake planner: moves the first scoped unit(s); everything else is core."""
    topic = topic or re.sub(r'[^a-z0-9]+', '-', target.removeprefix('references/').removesuffix('.md').lower()).strip('-')
    chosen = [u for u in data['units'] if (select or scoped)(u)][:limit]
    ids = {u['id'] for u in chosen}
    return {'schema': 2, 'input_hash': data['input_hash'],
            'classifications': [{'unit_id': u['id'], 'class': 'specific' if u['id'] in ids else 'core',
                                 'topic': topic if u['id'] in ids else '', 'reason': 'fixture classification'}
                                for u in data['units']],
            'operations': [
        {'unit_id': u['id'], 'target': target, 'topic': topic, 'applies_when': f'the task involves {topic}',
         'requires': [], 'reason': 'The complete scoped unit is retained verbatim.'}
        for u in chosen]}


def payload(files):
    return {'input_hash': digest(files),
            'units': [u.record() for u in parse_experience_units(files['SKILL.md']) if not u.protected]}


def approve(data, reject=()):
    """Fake reviewer: per-operation verdicts for migration, a single verdict for P3."""
    if 'operations' not in data:
        return {'approved': True, 'conflicts': [], 'uncertainties': [], 'reason': 'Exact content and conditions retained.'}
    return {'reason': 'Audited each operation.', 'decisions': [
        {'unit_id': op['unit_id'], 'approved': op['unit_id'] not in reject,
         'conflicts': ['fixture conflict'] if op['unit_id'] in reject else [], 'uncertainties': [],
         'reason': 'fixture decision'} for op in data['operations']]}


def reject_all(data, uncertainty='Caller scope ambiguous'):
    return {'reason': 'Cannot establish coverage.', 'decisions': [
        {'unit_id': op['unit_id'], 'approved': False, 'conflicts': [], 'uncertainties': [uncertainty],
         'reason': 'rejected'} for op in data['operations']]}


class PlannerModels:
    def __init__(self, planner=None, reviewer=None):
        self.planner = planner or plan_for
        self.reviewer = reviewer or approve
        self.requests, self.options = [], []

    def __call__(self, config):
        self.options.append(config)
        owner = self
        class Client:
            n_calls = 0
            def query(self, messages, **kwargs):
                self.n_calls += 1
                owner.requests.append(messages)
                data = json.loads(messages[1]['content'])
                value = owner.planner(data) if messages[0]['content'].startswith('Propose') else owner.reviewer(data)
                return {'content': value if isinstance(value, str) else json.dumps(value)}
            def stats(self):
                return {'calls': self.n_calls, 'input_tokens': 10*self.n_calls,
                        'output_tokens': 5*self.n_calls, 'cost_usd': 0}
        return Client()


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base, _ = load_initial_skill(directory_schema=1, bootstrap=True)
        self.config = {'skill_directory': {'schema': 1}, 'skill_update_method': 'trace2skill',
                       'skill_maintenance': settings({'skill_maintenance': {'enabled': True}}),
                       'skill_editor_model': {'name': 'mock', 'num_retries': 2}}
        self.models = PlannerModels()

    def long_root(self):
        return self.base['SKILL.md'] + '\n' + ''.join(section(f'Click case {i}') for i in range(3))

    def maintain(self, text=None, changes=None, folder='maintenance'):
        return maintain_candidate(self.base, changes if changes is not None else {'SKILL.md': text},
                                  self.config, self.root / folder, model_factory=self.models)

    def test_9999_10000_no_migration_10001_triggers(self):
        for size in (9999, 10000, 10001):
            prefix = self.base['SKILL.md'] + '\n' + section(size=500)
            text = prefix.rstrip('\n') + 'x' * (size - len(prefix.rstrip('\n')) - 1) + '\n'
            self.assertEqual(len(text), size)
            if size <= 10000:
                result = self.maintain(text, folder=str(size))
                self.assertEqual(result['files']['SKILL.md'], text)
                self.assertEqual(self.models.requests, [])
            else:
                # The oversized single atom cannot fit a reference: attempted, rejected.
                with self.assertRaises(MaintenanceRejected):
                    self.maintain(text, folder=str(size))
                self.assertGreater(len(self.models.requests), 0)

    def test_success_new_reference_hash_coverage_and_protected_core(self):
        source = self.long_root()
        self.assertGreater(len(source), 10000)
        result = self.maintain(source)
        files = result['files']
        self.assertLessEqual(len(files['SKILL.md']), 10000)
        self.assertTrue(files['SKILL.md'].startswith(self.base['SKILL.md']))
        entry = validate_index(files)['entries'][0]
        self.assertEqual(entry['content_sha256'], sha(files[entry['path']]))
        coverage = read_json(self.root / 'maintenance/coverage.json')['events'][0]['coverage'][0]
        self.assertEqual(sha(source[coverage['start']:coverage['end']]), coverage['source_sha256'])
        self.assertEqual(self.models.options[0]['num_retries'], 0)
        usage = read_json(self.root / 'maintenance/usage.json')
        self.assertEqual(usage['attempted_calls'], 2)
        self.assertEqual(usage['total']['input_tokens'], 20)

    def test_existing_reference_preserved_and_provenance_not_fabricated(self):
        old = '# Existing\nWhen `click.Option` has defaults, preserve them.\n'
        self.base['references/click.md'] = old
        self.base[INDEX_PATH] = build_index(self.base)
        result = self.maintain(self.long_root())
        self.assertTrue(result['files']['references/click.md'].startswith(old))
        self.assertEqual(validate_index(result['files'])['entries'][0]['provenance'], [{'evidence_kind': 'legacy_unknown'}])

    def test_overflow_existing_reference_uses_independent_sibling(self):
        old = '# Existing\n' + 'x'*4400 + '\n'
        self.base['references/click.md'] = old
        self.base[INDEX_PATH] = build_index(self.base)
        files = self.maintain(self.long_root())['files']
        self.assertEqual(files['references/click.md'], old)
        self.assertIn('references/click-2.md', files)
        self.assertTrue(files['references/click-2.md'].startswith('# click\n\nApplies when: '))
        self.assertTrue(all(len(v) <= 5000 for p,v in files.items() if p.startswith('references/')))

    def test_protected_and_uncertain_units_never_move(self):
        core = '## Protected Core\nWhen `click.Option` is used, for all tasks preserve safety.\n' + 'z'*10000 + '\n'
        front = self.base['SKILL.md'].split('# Test and diagnose')[0]
        with self.assertRaisesRegex(MaintenanceRejected, 'no safely'):
            self.maintain(front + core, folder='only-core')
        self.assertEqual(self.models.requests, [])
        with self.assertRaises(MaintenanceRejected):
            self.maintain(self.base['SKILL.md'] + '\n' + core, folder='mixed')
        offered = [u for r in self.models.requests if r[0]['content'].startswith('Propose')
                   for u in json.loads(r[1]['content'])['units']]
        self.assertTrue(offered)
        self.assertFalse(any('Protected Core' in u['heading_path'] for u in offered))

    def test_mixed_parent_retained_child_moves_with_dependencies(self):
        parent = '## Mixed workflow\nFor all tasks, verify before and after changes.\n'
        child = section('Scoped example').replace('## ', '### ', 1)
        source = self.base['SKILL.md'] + '\n' + parent + child + section('Other') + section('Third')
        result = self.maintain(source)['files']
        self.assertIn(parent, result['SKILL.md'])
        self.assertIn('### Scoped example', result['SKILL.md'])
        self.assertNotIn('For all tasks', result['references/click.md'])
        self.assertIn('Unless the value is missing', result['references/click.md'])

    def test_required_units_stay_or_move_to_the_same_target(self):
        files = self.base | {'SKILL.md': self.long_root()}
        data = payload(files)
        plan = plan_for(data, limit=2)
        first, second = plan['operations']
        first['requires'] = [second['unit_id']]
        second.update(target='references/other.md', topic='other')
        for item in plan['classifications']:
            if item['unit_id'] == second['unit_id']:
                item['topic'] = 'other'
        after, coverage, _, issues = apply_plan(files, plan, 5000)
        self.assertEqual([r['unit_id'] for r in coverage], [second['unit_id']])
        self.assertEqual(issues, [{'unit_id': first['unit_id'], 'error': 'dependent units must stay or move together'}])
        self.assertIn(next(u['text'] for u in data['units'] if u['id'] == first['unit_id']), after['SKILL.md'])
        plan = plan_for(data, limit=1)
        plan['operations'][0]['requires'] = [data['units'][0]['id']]
        after, coverage, _, issues = apply_plan(files, plan, 5000)
        self.assertEqual(issues, [])
        self.assertIn(data['units'][0]['text'], after['SKILL.md'])

    def test_invalid_operations_are_dropped_individually(self):
        files = self.base | {'SKILL.md': self.long_root()}
        plan = plan_for(payload(files), limit=2)
        plan['operations'][1]['target'] = 'references/elsewhere.md'
        _, coverage, _, issues = apply_plan(files, plan, 5000)
        self.assertEqual(len(coverage), 1)
        self.assertEqual(issues[0]['error'], 'one topic must use one target')
        plan = plan_for(payload(files), limit=2)
        plan['operations'][1]['topic'] = 'second'
        next(c for c in plan['classifications'] if c['unit_id'] == plan['operations'][1]['unit_id'])['topic'] = 'second'
        with self.assertRaisesRegex(UnsafeMarkdown, 'mix topics'):
            apply_plan(files, plan, 5000)
        plan = plan_for(payload(files))
        chosen = plan['operations'][0]['unit_id']
        plan['classifications'] = [c for c in plan['classifications'] if c['unit_id'] != chosen]
        _, coverage, labels, issues = apply_plan(files, plan, 5000, require_shrink=False)
        self.assertEqual(coverage, [])
        self.assertEqual(next(c for c in labels if c['unit_id'] == chosen)['class'], 'uncertain')
        self.assertEqual(issues[0]['error'], 'unit is not classified specific under this topic')

    def test_small_new_reference_is_dropped_but_existing_reference_accepts_it(self):
        files = self.base | {'SKILL.md': self.long_root()}
        plan = plan_for(payload(files))
        size = len(next(u for u in payload(files)['units'] if u['id'] == plan['operations'][0]['unit_id'])['text'])
        _, coverage, _, issues = apply_plan(files, plan, 5000, require_shrink=False,
                                            min_new_reference_chars=size + 10)
        self.assertEqual(coverage, [])
        self.assertIn('minimum', issues[0]['error'])
        with self.assertRaisesRegex(UnsafeMarkdown, 'minimum'):
            apply_plan(files, plan, 5000, min_new_reference_chars=size + 10)
        self.assertEqual(len(apply_plan(files, plan, 5000, min_new_reference_chars=size - 10)[1]), 1)
        files['references/click.md'] = '# Click\n\nApplies when: the task involves click\n'
        plan = plan_for(payload(files))
        after, coverage, _, issues = apply_plan(files, plan, 5000, min_new_reference_chars=size + 10)
        self.assertEqual((len(coverage), issues), (1, []))
        self.assertTrue(after['references/click.md'].startswith(files['references/click.md']))

    def test_last_mandatory_round_allows_small_new_reference(self):
        self.config['skill_maintenance'] = settings({'skill_maintenance': {
            'enabled': True, 'min_new_reference_chars': 4999}})
        files = self.maintain(self.long_root())['files']
        self.assertLessEqual(len(files['SKILL.md']), 10000)
        self.assertIn('references/click.md', files)
        events = read_json(self.root / 'maintenance/coverage.json')['events']
        self.assertEqual([e['kind'] for e in events], ['round_rejected', 'round_rejected', 'root_migration'])
        self.assertIn('minimum 4999', events[0]['reason'])
        self.assertEqual(events[2]['min_new_reference_chars'], 0)
        plans = [json.loads(m[1]['content']) for m in self.models.requests if m[0]['content'].startswith('Propose')]
        self.assertEqual([p['min_new_reference_chars'] for p in plans], [4999, 4999, 0])

    def test_program_copies_text_and_ignores_model_supplied_copies(self):
        files = self.base | {'SKILL.md': self.long_root()}
        data = payload(files)
        plan = plan_for(data)
        plan['operations'][0] |= {'target_content': 'a summary', 'source_sha256': 'bad', 'start': 0}
        after, coverage, _, issues = apply_plan(files, plan, 5000)
        unit = next(u for u in data['units'] if u['id'] == plan['operations'][0]['unit_id'])
        self.assertEqual(issues, [])
        self.assertIn(unit['text'].strip(), after['references/click.md'])
        self.assertNotIn('a summary', after['references/click.md'])
        self.assertEqual(coverage[0]['source_sha256'], unit['content_sha256'])

    def test_fences_lists_quotes_setext_are_not_split(self):
        text = '# Core\nIntro\n\nTopic\n-----\nWhen `pytest.fixture` is changed, retain scope.\n'
        text += '1. First\n   - Nested list\n> quoted condition\n```python\n## not a heading\nprint(1)\n```\n'
        units = parse_units(text)
        self.assertEqual(len(units), 1)
        self.assertIn('## not a heading', units[0].text)
        self.assertEqual(units[0].heading_path, ('Core', 'Topic'))
        with self.assertRaises(UnsafeMarkdown):
            parse_units(text + '```python\n')
        code_only = '# Core\n## Example\n```text\nWhen `pytest.fixture` changes, act.\n```\n'
        self.assertEqual(parse_units(code_only)[0].applicability, ())

    def test_experience_units_keep_nested_lists_fences_and_continuations(self):
        text = ('# Workflow\nIntro paragraph\nstill intro.\n\n1. First step\n   - nested detail\n\n'
                '   continued paragraph of step one.\n7a. Sub-numbered step\n```python\n## not a heading\n```\n'
                '- Bullet after\n\n    indented code\n\nClosing paragraph.\n## Protected Core\n- kept\n')
        units = parse_experience_units(text)
        self.assertEqual([u.kind for u in units], ['paragraph', 'item', 'item', 'item', 'paragraph', 'item'])
        self.assertIn('continued paragraph of step one.', units[1].text)
        self.assertIn('## not a heading', units[2].text)
        self.assertIn('indented code', units[3].text)
        self.assertEqual([u.protected for u in units], [True] + [False] * 4 + [True])
        self.assertEqual(''.join(u.text for u in units[:5]), text[text.index('Intro'):text.index('## Protected')])
        self.assertEqual(units[1].fingerprint, sha(units[1].text.strip()))

    def test_brackets_inside_code_spans_are_not_links(self):
        text = '7. Use `d["key"]` and `config[name]` before [core](#test-and-diagnose).\n'
        self.assertIn('`d["key"]`', rebase_links(text, 'SKILL.md', 'references/x.md', self.base))
        with self.assertRaisesRegex(UnsafeMarkdown, 'shortcut'):
            rebase_links('7. Read [details] first.\n', 'SKILL.md', 'references/x.md', self.base)

    def test_skill_scope_and_fixed_tool_units_are_protected(self):
        units = parse_experience_units(self.base['SKILL.md'])
        protected = [u.text.split('\n')[0][:30] for u in units if u.protected]
        self.assertEqual(protected, ['Use for SWE-bench software-rep', '2. Run `python /opt/rsi-skills',
                                     '4. Prefer the smallest relevan', 'The helper reads configuration'])
        nested = parse_experience_units('# Top\n## Topic\nIntro under level two.\n- Read `scripts/x.py`.\n')
        self.assertEqual([u.protected for u in nested], [False, True])

    def test_each_moved_item_keeps_its_number_in_place(self):
        root = self.base['SKILL.md'] + '\n## Notes\n\n' + ''.join(
            f'{n}. When `click.Option` handles case {n}, keep `ctx.params`.\n' for n in ('6', '7a', '8')) + '9. Keep going.\n'
        files = self.base | {'SKILL.md': root}
        plan = plan_for(payload(files), limit=3, select=lambda u: 'click.Option' in u['text'])
        after, coverage, _, _ = apply_plan(files, plan, 5000, require_shrink=False)
        lines = [line for line in after['SKILL.md'].splitlines() if line[:1].isdigit()][-4:]
        self.assertEqual(lines, ['6. Specific guidance (the task involves click): [Click](references/click.md)',
                                 '7a. Specific guidance (the task involves click): [Click](references/click.md)',
                                 '8. Specific guidance (the task involves click): [Click](references/click.md)',
                                 '9. Keep going.'])
        validate_coverage(files, after, coverage)
        offered = [u.text for u in parse_experience_units(after['SKILL.md']) if not u.protected]
        self.assertFalse(any('Specific guidance' in text for text in offered))

    def test_relative_links_rebased_anchor_navigation_retained(self):
        linked = section().rstrip('\n') + ' Read [core](#test-and-diagnose).\n'
        tool = section('Tool use').rstrip('\n') + ' Run [tool](scripts/inspect_project.py).\n'
        source = self.base['SKILL.md'] + '\n' + linked + tool + section('Third')
        files = self.maintain(source)['files']
        target = files['references/click.md']
        self.assertIn('(../SKILL.md#test-and-diagnose)', target)
        self.assertIn('Run [tool](scripts/inspect_project.py)', files['SKILL.md'])
        self.assertIn('## Click options', files['SKILL.md'])
        self.assertIn('\nSpecific guidance (the task involves click): [Click](references/click.md)\n', files['SKILL.md'])
        entry = validate_index(files)['entries'][0]
        self.assertEqual((entry['title'], entry['applies_when']), ('Click', ['the task involves click']))

    def test_ambiguous_relative_code_and_reference_links_rejected(self):
        for extra in ('```bash\ncat references/testing.md\n```\n', '[details][ref]\n'):
            source = self.base['SKILL.md'] + '\n' + section() + extra + section('Second') + section('Third')
            with self.assertRaises(MaintenanceRejected):
                self.maintain(source, folder=str(len(extra)))

    def test_reference_split_keeps_source_navigation_and_full_topics(self):
        ref = '# Topics\nShared precondition.\n' + section('One', 2700) + section('Two', 2700)
        result = self.maintain(changes={'references/big.md': ref})['files']
        self.assertLessEqual(len(result['references/big.md']), 5000)
        targets = [p for p in result if p.startswith('references/split-')]
        self.assertEqual(len(targets), 2)
        for path in targets:
            self.assertIn('Shared precondition.', result[path])
            self.assertIn('Unless the value is missing', result[path])
        validate_index(result)
        self.assertEqual(len(self.models.requests), 1)
        self.assertTrue(self.models.requests[0][0]['content'].startswith('Independently audit'))

    def test_indivisible_reference_rejected(self):
        with self.assertRaises(MaintenanceRejected):
            self.maintain(changes={'references/big.md': '# Huge\n' + 'x'*6000 + '\n'})

    def test_invalid_plans_and_nonconvergence_are_bounded(self):
        mutations = [lambda p:p.update(input_hash='bad'),
                     lambda p:p['operations'][0].update(target='../bad.md'),
                     lambda p:p['operations'][0].pop('applies_when'),
                     lambda p:p['operations'][0].update(unit_id='unknown'),
                     lambda p:p['operations'][0].update(topic='other'),
                     lambda p:p['operations'][0].update(applies_when='see [x](y)'),
                     lambda p:p['operations'][0].update(requires=['unknown']),
                     lambda p:p.update(classifications=[]),
                     lambda p:p.update(schema=1),
                     lambda p:p.update(operations=[])]
        for i, mutate in enumerate(mutations):
            def invalid(data, mutate=mutate):
                value = plan_for(data); mutate(value); return value
            self.models = PlannerModels(planner=invalid)
            with self.subTest(i=i), self.assertRaises(MaintenanceRejected):
                self.maintain(self.long_root(), folder=f'invalid-{i}')
            self.assertEqual(len(self.models.requests), 3)

    def test_invalid_json_and_call_budget(self):
        self.models = PlannerModels(planner=lambda data: '{')
        self.config['skill_maintenance']['max_llm_calls'] = 1
        with self.assertRaises(MaintenanceRejected):
            self.maintain(self.long_root())
        self.assertEqual(len(self.models.requests), 1)

    def test_independent_review_uncertainty_rejects_candidate(self):
        self.models = PlannerModels(reviewer=reject_all)
        with self.assertRaises(MaintenanceRejected):
            self.maintain(self.long_root())
        self.assertEqual(len(self.models.requests), 6)
        self.assertEqual(read_json(self.root/'maintenance/validation.json')['status'], 'rejected')
        # The next plan sees the reviewer's concrete objection, not just a generic rejection.
        retry = json.loads(self.models.requests[2][1]['content'])
        self.assertIn('Caller scope ambiguous', retry['previous_error'])

    def test_partial_review_applies_only_approved_operations(self):
        source = self.base['SKILL.md'] + '\n' + ''.join(section(f'Click case {i}', 2400) for i in range(5))
        refused = []
        def reviewer(data):
            if not refused:
                refused.append(data['operations'][0]['unit_id'])
            return approve(data, reject=refused)
        self.models = PlannerModels(planner=lambda data: plan_for(data, limit=3), reviewer=reviewer)
        files = self.maintain(source)['files']
        events = read_json(self.root/'maintenance/coverage.json')['events']
        first = events[0]
        self.assertEqual([r['unit_id'] for r in first['rejected_operations']], refused)
        self.assertEqual(len(first['coverage']), 2)
        self.assertNotIn(refused[0], [r['unit_id'] for r in first['coverage']])
        retry = json.loads(self.models.requests[2][1]['content'])
        self.assertIn('fixture conflict', retry['previous_error'])
        self.assertLessEqual(len(files['SKILL.md']), 10000)

    def test_invalid_review_shape_rejects(self):
        for verdict in ({'approved': True, 'conflicts': [], 'uncertainties': [], 'reason': 'old schema'},
                        {'reason': 'missing decision', 'decisions': []}):
            with self.subTest(verdict=verdict):
                self.models = PlannerModels(reviewer=lambda data, v=verdict: v)
                with self.assertRaises(MaintenanceRejected):
                    self.maintain(self.long_root(), folder=str(len(verdict)) + verdict['reason'][:3])

    def test_api_error_not_disguised_as_rejection(self):
        def fail(data):
            raise ConnectionError('service unavailable')
        self.models = PlannerModels(planner=fail)
        with self.assertRaises(ConnectionError):
            self.maintain(self.long_root())
        self.assertEqual(read_json(self.root/'maintenance/validation.json')['status'], 'error')
        self.assertEqual(len(self.models.requests), 1)

    def test_timeout_is_infrastructure_failure(self):
        clock = [0]
        def slow(data):
            clock[0] = 1201
            return plan_for(data)
        self.models = PlannerModels(planner=slow)
        with patch('rsi.skills.maintenance.time.monotonic', side_effect=lambda:clock[0]), self.assertRaises(TimeoutError):
            self.maintain(self.long_root())
        self.assertEqual(read_json(self.root/'maintenance/validation.json')['status'], 'error')

    def test_exact_coverage_detects_loss_or_rewrite(self):
        files = self.base | {'SKILL.md': self.long_root()}
        after, coverage, _, _ = apply_plan(files, plan_for(payload(files), limit=2), 5000)
        validate_coverage(files, after, coverage)
        for mutate in (lambda a: a.update({'references/click.md': a['references/click.md'].replace('Unless', 'Always')}),
                       lambda a: a.update({'SKILL.md': a['SKILL.md'].replace('Specific guidance', 'Guidance')}),
                       lambda a: a.update({'SKILL.md': a['SKILL.md'].replace('1. Inspect', '1. Look at')})):
            changed = dict(after)
            mutate(changed)
            with self.subTest(), self.assertRaises(UnsafeMarkdown):
                validate_coverage(files, changed, coverage)

    def test_canonicalization_and_disabled_settings(self):
        text = '\ufeff' + self.base['SKILL.md'].replace('\n','\r\n') + '\r\n\r\n'
        files = self.maintain(text)['files']
        self.assertEqual(files['SKILL.md'], canonical(text))
        self.assertTrue(read_json(self.root/'maintenance/normalization.json')[0]['removed_bom'])
        self.assertEqual(self.models.requests, [])
        validate_limits({'SKILL.md': 'x'*10001}, {'enabled': False})
        self.assertEqual(directory_schema({}), 0)

    def test_strict_configuration_types_and_ranges(self):
        for field, value in [('enabled', 1), ('max_llm_calls', True), ('max_migration_rounds', 0),
                             ('timeout_seconds', float('inf')), ('max_reference_chars', -1),
                             ('root_target_chars', 10000), ('min_new_reference_chars', -1),
                             ('min_new_reference_chars', 5000), ('min_new_reference_chars', 1.5), ('unknown', 1)]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                settings({'skill_maintenance': {field: value}})
        with self.assertRaises(ValueError):
            directory_schema({'skill_maintenance': {'enabled':True}})
        with self.assertRaises(ValueError):
            directory_schema(self.config | {'skill_update_method':'reasoningbank'})

    def test_store_limits_gate_and_rejection_leave_pointer(self):
        store = SkillStore(self.root/'store', self.base, directory_schema=1, maintenance=self.config['skill_maintenance'])
        current = store.current()
        with self.assertRaises(SkillProposalRejected):
            store.propose({'SKILL.md': self.long_root()})
        self.assertEqual(store.current(), current)
        result = self.maintain(self.long_root())
        candidate, _ = store.propose(result['changes'], managed_changes=result['managed_changes'])
        self.assertEqual(store.current(), current)
        self.assertTrue(document_gate(candidate, store.fixed_files, directory_schema=1,
                                     maintenance=self.config['skill_maintenance'])['passed'])
        store.record(candidate, True, {})
        self.assertEqual(store.current(), candidate)

    def test_nested_protected_core_and_indented_code_stay_put(self):
        text = '# Toolkit\n## Protected Core\n### Click contract\nWhen `click.Option` changes, preserve safety.\n'
        self.assertTrue(parse_units(text)[0].protected)
        text = '# Toolkit\n## Example\n    When `click.Option` changes, this is code.\n'
        self.assertEqual(parse_units(text)[0].applicability, ())

    def test_ambiguous_cross_section_html_and_inline_examples(self):
        for text in ('See above for prerequisites.\n', '[shortcut]\n', '<!-- context -->\n',
                     'Example: `[tool](scripts/inspect_project.py)`\n'):
            with self.subTest(text=text), self.assertRaises(UnsafeMarkdown):
                rebase_links(text, 'SKILL.md', 'references/new.md', self.base)

    def test_unknown_classification_cannot_override_coverage(self):
        def unknown(data):
            plan = plan_for(data)
            for item in plan['classifications']:
                item['class'] = 'uncertain'
            return plan
        self.models = PlannerModels(planner=unknown)
        with self.assertRaises(MaintenanceRejected):
            self.maintain(self.long_root())
        self.assertEqual(len(self.models.requests), 3)

    def test_no_progress_plan_cannot_publish(self):
        files = self.base | {'SKILL.md': '# Repair notes\n## X\nIf `x.y`, act.\n'}
        with self.assertRaisesRegex(UnsafeMarkdown, 'no progress'):
            apply_plan(files, plan_for(payload(files)), 5000)
        after, coverage, _, _ = apply_plan(files, plan_for(payload(files)), 5000, require_shrink=False)
        self.assertEqual(len(coverage), 1)
        empty = plan_for(payload(files), limit=0)
        self.assertEqual(apply_plan(files, empty, 5000, require_shrink=False)[0], files)

    def test_soft_target_continues_after_hard_limit_is_met(self):
        source = self.base['SKILL.md'] + '\n' + ''.join(section(f'Topic {i}', 2200) for i in range(5))
        files = self.maintain(source)['files']
        self.assertLessEqual(len(files['SKILL.md']), 8000)
        self.assertGreaterEqual(len(self.models.requests), 4)

    def test_append_retains_existing_evidence_identity_without_new_pass_claim(self):
        old = '# Existing\nWhen `click.Option` changes, preserve defaults.\n'
        self.base['references/click.md'] = old
        import yaml
        index = validate_index(self.base | {INDEX_PATH:build_index(self.base)})
        origin = {'task_id':'fixture-only', 'rollout':0, 'evidence_id':'a'*64, 'evidence_kind':'success'}
        index['entries'][0]['provenance'] = [origin]
        self.base[INDEX_PATH] = yaml.safe_dump(index)
        files = self.maintain(self.long_root())['files']
        origins = validate_index(files)['entries'][0]['provenance']
        self.assertEqual(origins, [origin, {'evidence_kind':'legacy_unknown'}])

    def test_disk_failure_is_not_candidate_rejection(self):
        original = Path.write_text
        def fail(path, *args, **kwargs):
            if path.name == 'SKILL.md' and path.parent.name.startswith('rsi-maintenance-'):
                raise OSError('injected disk failure')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'write_text', fail), self.assertRaises(OSError):
            self.maintain(self.base['SKILL.md'])
        self.assertEqual(read_json(self.root/'maintenance/validation.json')['status'], 'error')

    def test_frontmatter_rejection_distinct_from_validator_runtime_error(self):
        with self.assertRaises(MaintenanceRejected):
            self.maintain('# Missing required frontmatter\n')
        from types import SimpleNamespace
        failed = SimpleNamespace(returncode=1, stdout='', stderr='Traceback: missing dependency')
        with patch('rsi.skills.maintenance.subprocess.run', return_value=failed), self.assertRaises(RuntimeError):
            self.maintain(self.base['SKILL.md'], folder='validator-error')
        self.assertEqual(read_json(self.root/'validator-error/validation.json')['status'], 'error')


    def test_reference_split_rejected_on_dependency_uncertainty(self):
        self.models = PlannerModels(reviewer=lambda data: approve(data, reject={data['operations'][0]['unit_id']}))
        ref = '# Topics\nShared prerequisite.\n' + section('One', 2700) + section('Two', 2700)
        with self.assertRaises(MaintenanceRejected):
            self.maintain(changes={'references/big.md': ref})
        self.assertEqual(len(self.models.requests), 1)


    def test_duplicate_json_keys_are_invalid(self):
        self.models = PlannerModels(planner=lambda data:'{"schema":0,"schema":1,"input_hash":"x","operations":[]}')
        with self.assertRaises(MaintenanceRejected):
            self.maintain(self.long_root())
        self.assertEqual(len(self.models.requests), 3)


    def test_nontext_candidate_is_a_controlled_rejection(self):
        with self.assertRaises(MaintenanceRejected):
            self.maintain(changes={'SKILL.md':42})
        self.assertEqual(read_json(self.root/'maintenance/validation.json')['status'], 'rejected')


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        import test_maintenance_transactions as p0
        p0.ManagedCheckpointTests.setUp(self)
        self.config['skill_maintenance'] = settings({'skill_maintenance': {'enabled': True}})
        self.base, _ = load_initial_skill(directory_schema=1, bootstrap=True)
        self.long = self.base['SKILL.md'] + '\n' + ''.join(section(f'Click case {i}') for i in range(3))

    def run_tasks(self, resume=False):
        from rsi.agent.runner import run_experiment
        from test_core_baseline import Backend
        return run_experiment(self.tasks, self.config, self.output, resume=resume,
                              backend_factory=Backend, model_factory=self.models)

    def hybrid(self, *, api_error=False, editor_long=False):
        from test_core_baseline import Models
        source = self.long
        class Hybrid(Models):
            def __call__(owner, config):
                client = super().__call__(config)
                original = client.query
                def query(messages, **kwargs):
                    prompt = messages[0]['content']
                    if prompt.startswith(('Propose a lossless', 'Independently audit')):
                        owner.requests.append(('maintenance', copy.deepcopy(messages)))
                        client.n_calls += 1
                        if api_error:
                            raise ConnectionError('service unavailable')
                        data = json.loads(messages[1]['content'])
                        value = plan_for(data) if prompt.startswith('Propose') else approve(data)
                        return {'content': json.dumps(value)}
                    answer = original(messages, **kwargs)
                    if editor_long and client.name == 'judge' and answer['content'].startswith(chr(96)*3+'json'):
                        data = json.loads(answer['content'][7:-3])
                        data['edits'] = [{'file':'SKILL.md', 'op':'append_to_section',
                            'target_section':'# Test and diagnose',
                            'content':'\n' + ''.join(section(f'Click case {i}') for i in range(3))}]
                        answer['content'] = chr(96)*3+'json\n'+json.dumps(data)+'\n'+chr(96)*3
                    return answer
                client.query = query
                return client
        return Hybrid()

    def proposal(self, text=None):
        return {'changes': {'SKILL.md': text or self.long}, 'reason': 'supported scopes', 'evidence_rollouts': list(range(5))}

    def test_real_upstream_then_migration_and_next_task_inheritance(self):
        self.models = self.hybrid(editor_long=True)
        self.tasks.append({'id':'second', 'benchmark':'swe', 'prompt':'Fix another issue'})
        reports = self.run_tasks()
        update = read_json(self.output/'task/update.json')
        self.assertTrue(update['accepted'])
        self.assertEqual(update['maintenance']['status'], 'accepted')
        self.assertEqual(reports[1]['skill_version'], update['after_skill_version'])
        files = read_skill_directory(self.output/'skills/versions'/update['after_skill_version'], directory_schema=1)
        self.assertLessEqual(len(files['SKILL.md']), 10000)
        self.assertTrue(validate_index(files)['entries'])
        self.assertGreater(update['maintenance_usage']['input_tokens'], 0)

    def test_mandatory_rejection_commits_scores_but_keeps_skill(self):
        from rsi.experiment import checkpoints
        bad = self.base['SKILL.md'] + '\n' + 'generic protected prose '*500 + '\n'
        with patch('rsi.agent.runner.evolve_skill', return_value=self.proposal(bad)):
            reports = self.run_tasks()
        state, _, _ = checkpoints.resume_request(self.output)
        update = state['completed'][0]['update']
        self.assertEqual(update['decision'], 'checks_failed')
        self.assertEqual(update['maintenance']['status'], 'rejected')
        self.assertEqual(state['current_skill_version'], state['initial_skill_version'])
        self.assertTrue(reports[0]['supervision_complete'])
        self.assertEqual(len(read_json(self.output/'task/all-route-scores.json')['scores']), 5)

    def test_api_failure_uncommitted_resume_archives_scores_and_attempt(self):
        from rsi.experiment import checkpoints
        self.models = self.hybrid(api_error=True)
        with patch('rsi.agent.runner.evolve_skill', return_value=self.proposal()):
            with self.assertRaises(ConnectionError):
                self.run_tasks()
            state, _, _ = checkpoints.resume_request(self.output)
            self.assertEqual(state['completed'], [])
            old_scores = (self.output/'task/all-route-scores.json').read_bytes()
            self.models = self.hybrid()
            self.run_tasks(resume=True)
        archived = list((self.output/'attempts').iterdir())
        self.assertEqual(len(archived), 1)
        self.assertEqual((archived[0]/'all-route-scores.json').read_bytes(), old_scores)
        self.assertEqual(read_json(archived[0]/'maintenance/validation.json')['status'], 'error')

    def test_commit_then_publish_failure_resume_does_not_repeat_migration(self):
        self.models = self.hybrid()
        with patch('rsi.agent.runner.evolve_skill', return_value=self.proposal()), \
             patch('rsi.experiment.checkpoints.publish', side_effect=OSError('publish failed')), self.assertRaises(OSError):
            self.run_tasks()
        coverage = (self.output/'task/maintenance/coverage.json').read_bytes()
        self.models.requests.clear()
        self.run_tasks(resume=True)
        self.assertEqual(self.models.requests, [])
        self.assertEqual((self.output/'task/maintenance/coverage.json').read_bytes(), coverage)
        self.assertEqual(len(read_json(self.output/'checkpoint.json')['completed']), 1)

    def test_precommit_failure_does_not_move_pointer(self):
        self.models = self.hybrid()
        with patch('rsi.agent.runner.evolve_skill', return_value=self.proposal()), \
             patch('rsi.agent.runner.commit', side_effect=OSError('before commit')), self.assertRaises(OSError):
            self.run_tasks()
        state = read_json(self.output/'checkpoint.json')
        self.assertEqual(state['completed'], [])
        self.assertEqual(read_json(self.output/'skills/current.json')['version'], state['initial_skill_version'])
        self.assertEqual(read_json(self.output/'task/maintenance/validation.json')['status'], 'accepted')

    def test_disabled_keeps_long_p0_skill_without_maintenance(self):
        self.config['skill_maintenance']['enabled'] = False
        with patch('rsi.agent.runner.evolve_skill', return_value=self.proposal()):
            self.run_tasks()
        self.assertFalse((self.output/'task/maintenance').exists())
        self.assertFalse((self.output/'bootstrap-maintenance').exists())
        version = read_json(self.output/'skills/current.json')['version']
        self.assertEqual((self.output/'skills/versions'/version/'SKILL.md').read_text(), self.long)

    def test_bootstrap_derives_seed_without_mutating_source(self):
        seed = self.base | {'SKILL.md': self.long}
        source = SkillStore(self.output.parent/'source', seed, directory_schema=1).current()
        before = read_skill_directory(source, directory_schema=1)
        self.config['initial_skill'] = str(source)
        self.models = self.hybrid()
        with patch('rsi.agent.runner.evolve_skill', return_value={'changes':{}, 'reason':'none'}):
            self.run_tasks()
        self.assertEqual(read_skill_directory(source, directory_schema=1), before)
        initial = read_json(self.output/'initial-skill-source.json')
        self.assertNotEqual(initial['derived_version'], initial['source_version'])
        self.assertEqual(read_json(self.output/'bootstrap-maintenance/validation.json')['status'], 'accepted')

    def test_resume_rejects_modified_coverage_artifact(self):
        from rsi.experiment import checkpoints
        self.models = self.hybrid()
        with patch('rsi.agent.runner.evolve_skill', return_value=self.proposal()):
            self.run_tasks()
        (self.output/'task/maintenance/coverage.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'artifact hash'):
            checkpoints.resume_request(self.output)

    def test_bootstrap_failure_is_marked_failed_without_checkpoint(self):
        seed = self.base | {'SKILL.md':self.base['SKILL.md'] + '\n' + 'global instructions '*600 + '\n'}
        source = SkillStore(self.output.parent/'source', seed, directory_schema=1).current()
        self.config['initial_skill'] = str(source)
        with self.assertRaises(MaintenanceRejected):
            self.run_tasks()
        self.assertEqual(read_json(self.output/'run-info.json')['status'], 'failed')
        self.assertFalse((self.output/'checkpoint.json').exists())
        self.assertEqual(read_skill_directory(source, directory_schema=1), seed)
