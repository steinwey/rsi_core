"""Offline repository-skill tests; no model API or Docker required."""
import contextlib
import io
import shlex
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from rsi.agent.execution import installable_skill
from rsi.benchmarks.backends import install_skill
from rsi.core.common import digest, read_json, write_json
from rsi.skills.budget import CounterInfo, SkillContext
from rsi.skills.directory import read_skill_directory
from rsi.skills.index import build_index
from rsi.skills.repository_skill import (
    RepositoryPool, compose, core_markdown, decide, rank_experiences, retrieve, task_query,
    task_repo, units_from_snapshot,
)
from rsi.skills.routing import RouteResult


class CharacterCounter:
    info = CounterInfo(model='test-model', identity='test-counter-v1', accuracy='test_only',
                       statement='Offline deterministic test counter, not a provider tokenizer')

    def count(self, text):
        return max(1, len(text) // 6) if text else 0


def config(budget=4000):
    return {'model': {'name': 'test-model'}, 'skill_editor_model': {'name': 'test-model'},
            'skill_directory': {'schema': 1}, 'skill_routing': {'enabled': True,
            'max_initial_skill_tokens': budget, 'top_k': 2, 'budget_mode': 'strict'},
            'repository_skill': {'enabled': True, 'recall_k': 50, 'max_blocks': 10}}


def seed():
    result = {'SKILL.md': '# Core workflow\nDiagnose and validate a local software defect.\n'}
    result['routing/index.yaml'] = build_index(result)
    return result


def task(repo, task_id, description):
    return {'id': task_id, 'prompt': description,
            'instance': {'repo': repo}, 'benchmark': 'swe'}


def add_experience(pool, repo, text, name='experience.md', run='training-run'):
    store = pool.store(repo)
    before = store.current()
    candidate, _ = store.propose({f'references/{name}': text}, expected_base_version=before.name)
    pool.record(repo, store, candidate, True, run, {'task_id': run})
    return store.current().name


class RepositorySkillTests(unittest.TestCase):
    def test_repository_isolation_in_both_training_orders(self):
        for order in [('pallets/click', 'sqlfluff/sqlfluff'),
                      ('sqlfluff/sqlfluff', 'pallets/click')]:
            with self.subTest(order=order), tempfile.TemporaryDirectory() as tmp:
                pool = RepositoryPool(Path(tmp) / 'pool', seed())
                content = {
                    'pallets/click': '# Click\n## Default map\nApplies when: default_map UNSET sentinel\n'
                                     'When default_map key contains UNSET, preserve sentinel behavior.\n',
                    'sqlfluff/sqlfluff': '# SQLFluff\n## Parsing\nApplies when: SQL parser whitespace\n'
                                         'For SQL parser whitespace errors, retain token boundaries.\n',
                }
                versions = {repo: add_experience(pool, repo, content[repo], run=f'{repo}-run') for repo in order}
                self.assertNotEqual(versions[order[0]], versions[order[1]])
                for repo in order:
                    current = pool.store(repo).current()
                    units = units_from_snapshot(current, next(m for _,m in pool.snapshots() if m['source_repo'] == repo))
                    self.assertTrue(any('references/experience.md' == u.source_path for u in units))
                    self.assertTrue(all(u.source_repo == repo for u in units))
                click = retrieve(task('pallets/click', 't-click', 'default_map UNSET sentinel'), pool, recall_k=20)
                sql = retrieve(task('sqlfluff/sqlfluff', 't-sql', 'SQL parser whitespace'), pool, recall_k=20)
                self.assertTrue(any(u.source_repo == 'pallets/click' for u in click))
                self.assertTrue(any(u.source_repo == 'sqlfluff/sqlfluff' for u in sql))
                self.assertEqual(pool.store(order[0]).current().name, versions[order[0]])
                self.assertEqual(pool.store(order[1]).current().name, versions[order[1]])

    def test_scope_and_cross_repo_condition(self):
        with tempfile.TemporaryDirectory() as tmp:
            pool = RepositoryPool(Path(tmp) / 'pool', seed())
            add_experience(pool, 'pallets/click',
                           '# Shared\n## Validation\nscope: *\nApplies when: regression pytest checks\n'
                           'Use pytest for regression checks after code changes.\n')
            found = retrieve(task('sqlfluff/sqlfluff', 't', 'regression pytest checks'), pool, recall_k=20)
            self.assertTrue(any(u.source_repo == 'pallets/click' for u in found))
            add_experience(pool, 'pallets/click',
                           '# Private\n## Sentinel\nApplies when: UNSET default_map\n'
                           'Click-specific sentinel instructions.\n', 'private.md')
            unseen = retrieve(task('sqlfluff/sqlfluff', 't2', 'UNSET default_map'), pool, recall_k=20)
            self.assertFalse(any(u.source_path == 'references/private.md' for u in unseen))

    def test_conflict_curator_and_source_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            pool = RepositoryPool(Path(tmp) / 'pool', seed())
            add_experience(pool, 'pallets/click',
                           '# First\n## Case A\nApplies when: public return boundary\n'
                           'Return UNSET from the public boundary.\n', 'a.md')
            add_experience(pool, 'pallets/click',
                           '# Second\n## Case B\nApplies when: public return boundary\n'
                           'Return None from the public boundary.\n', 'b.md')
            goal = 'Hide `Sentinel.UNSET` as `None` at the public return boundary.'
            t = task('pallets/click', 'click-case', 'Milestone goal:\n- ' + goal)
            found = retrieve(t, pool, recall_k=20)
            dropped = next(u for u in found if u.source_path == 'references/a.md')
            kept = next(u for u in found if u.source_path == 'references/b.md')
            chosen = {'experience_conflicts': [{
                'unit_ids': [dropped.unit_id, kept.unit_id],
                'quotes': {dropped.unit_id: 'Return UNSET from the public boundary.',
                           kept.unit_id: 'Return None from the public boundary.'},
                'conflict_point': 'public return value',
                'same_subject': True, 'same_condition': True, 'same_stage': True,
                'mutually_exclusive': True, 'keep_unit_id': kept.unit_id,
                'basis_quote': 'Hide `Sentinel.UNSET` as `None`',
                'reason': 'The task requires None at this same public return boundary.'}],
                      'task_conflicts': []}
            class FakeModel:
                def __init__(self, _: dict): pass
                def stats(self): return {'calls': 1, 'input_tokens': 10, 'output_tokens': 20}
            with patch('rsi.skills.repository_skill.generate_object', return_value=chosen) as gen:
                plan, usage = decide(t, found, config(), Path(tmp), model_factory=FakeModel)
            self.assertEqual(gen.call_count, 1)
            self.assertEqual(usage['calls'], 1)
            self.assertEqual(plan['excluded'][0]['unit_id'], dropped.unit_id)
            self.assertEqual(plan['proposals'][0]['status'], 'applied')
            snap = pool.store('pallets/click').current()
            ctx, runtime, receipt = compose(t, pool.snapshots(), seed(), plan, found, config(),
                                            Path(tmp) / 'route', counter_factory=lambda _: CharacterCounter())
            self.assertIn('Return None from the public boundary.', ctx.text)
            self.assertNotIn('Return UNSET from the public boundary.', ctx.text)
            self.assertIn('Source: pallets/click | b (Case B)', ctx.text)
            self.assertNotIn('Case A', ctx.text)
            self.assertEqual(receipt['selected'][0]['decision'], 'keep')
            self.assertEqual(receipt['selected'][0]['source_unit_ids'], [kept.unit_id])
            self.assertEqual(receipt['curator_excluded_ids'], [dropped.unit_id])
            self.assertEqual(len(receipt['selected'][0]['source_units']), 1)
            rendered = (runtime / 'SKILL.md').read_text(encoding='utf-8')
            front = yaml.safe_load(rendered.split('---', 2)[1])
            self.assertEqual(front['task_repo'], 'pallets/click')
            self.assertEqual(front['route_id'], receipt['route_id'])
            self.assertEqual(front['source_skill_versions']['pallets/click'], snap.name)
            self.assertEqual(receipt['context_hash'], ctx.context_hash)
            self.assertEqual(receipt['initial_skill_tokens'], CharacterCounter().count(ctx.text))
            self.assertTrue((runtime / 'routing/index.yaml').exists())

    def test_single_source_label_and_budget_include_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            pool = RepositoryPool(Path(tmp) / 'pool', seed())
            add_experience(pool, 'pallets/click',
                           '# Example\n## Sentinel handling\nApplies when: default_map UNSET\n'
                           'Preserve the sentinel only for the matching case.\n',
                           'sentinel-handling.md')
            t = task('pallets/click', 'source-budget', 'default_map UNSET')
            found = retrieve(t, pool, recall_k=20)
            unit = next(u for u in found if u.source_path == 'references/sentinel-handling.md')
            c = CharacterCounter()
            full, runtime, receipt = compose(
                t, pool.snapshots(), seed(), [], found, config(4000),
                Path(tmp) / 'with-source', counter_factory=lambda _: c)
            label = 'Source: pallets/click | sentinel-handling (Sentinel handling)\n'
            skill_text = (runtime / 'SKILL.md').read_text(encoding='utf-8')
            self.assertIn(label, skill_text)
            self.assertIn('Applies when: default_map UNSET', skill_text)
            self.assertIn('Preserve the sentinel only for the matching case.', skill_text)
            self.assertNotIn('## Guidance', skill_text)
            self.assertNotIn(unit.unit_id, skill_text)
            self.assertIn(unit.unit_id, receipt['selected'][0]['source_unit_ids'])
            self.assertEqual(c.count(full.text), full.initial_skill_tokens)
            # The exact same rendered text, INCLUDING the Source line, is counted
            # before admitting the block; a tighter budget must skip it.
            budget_without_label = c.count(full.text.replace(label, ''))
            self.assertLess(budget_without_label, full.initial_skill_tokens)
            short_config = config(budget_without_label)
            shorter, _, short_receipt = compose(
                t, pool.snapshots(), seed(), [], found, short_config,
                Path(tmp) / 'tight-budget', counter_factory=lambda _: c)
            self.assertEqual(short_receipt['selected'], [])
            self.assertEqual(short_receipt['skipped'][0]['reason'], 'block_over_budget')
            self.assertNotIn(label, shorter.text)

    def test_budget_skips_oversize_and_keeps_later_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            pool = RepositoryPool(Path(tmp) / 'pool', seed())
            add_experience(pool, 'pallets/click',
                           '# Guidance\n## Case A\nApplies when: click task\n' + ('X' * 100000) + '\n'
                           '## Case B\nApplies when: click task\nShort experience.\n')
            t = task('pallets/click', 'budget', 'click task')
            units = [u for u in retrieve(t, pool, recall_k=50) if u.source_path == 'references/experience.md']
            self.assertEqual(len(units), 2)
            ordered = sorted(units, key=lambda unit: 0 if 'X' * 100 in unit.markdown else 1)
            small = next(unit for unit in ordered if 'Short experience.' in unit.markdown)
            # The oversized original block is skipped whole; the later original block is kept.
            ctx, runtime, receipt = compose(t, pool.snapshots(), seed(), [], ordered, config(1000),
                                            Path(tmp)/'budget', counter_factory=lambda _: CharacterCounter())
            self.assertEqual(len(receipt['selected']), 1)
            self.assertEqual(receipt['selected'][0]['source_unit_ids'], [small.unit_id])
            self.assertEqual(receipt['budget_skipped_ids'], [ordered[0].unit_id])
            self.assertEqual(receipt['skipped'][0]['reason'], 'block_over_budget')
            self.assertNotIn('X' * 100, ctx.text)
            self.assertIn('Short experience.', ctx.text)
            self.assertNotIn('## Guidance', ctx.text)
            self.assertLessEqual(receipt['initial_skill_tokens'], receipt['effective_budget'])
            self.assertEqual(receipt['initial_skill_tokens'], CharacterCounter().count(ctx.text))

    def test_runtime_directory_installs_without_dropping_hash_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = seed()
            base['scripts/inspect_project.py'] = 'print("inspect")\n'
            base['routing/index.yaml'] = build_index(base)
            pool = RepositoryPool(root / 'pool', base)
            add_experience(pool, 'pallets/click',
                           '# Example\n## Sentinel handling\nApplies when: default_map UNSET\n'
                           'Preserve the sentinel only for the matching case.\n',
                           'sentinel-handling.md')
            t = task('pallets/click', 'install-case', 'default_map UNSET')
            found = retrieve(t, pool, recall_k=20)
            unit = next(u for u in found if u.source_path == 'references/sentinel-handling.md')
            _, runtime, _ = compose(t, pool.snapshots(), base, [], found, config(),
                                    root / 'route', counter_factory=lambda _: CharacterCounter())
            self.assertEqual(runtime.name, 'task-skill')
            self.assertIn('scripts/inspect_project.py', read_skill_directory(runtime, directory_schema=1))
            with self.assertRaisesRegex(ValueError, 'hash mismatch'):
                install_skill(LocalEnvironment(root / 'rejected'), runtime, directory_schema=1)
            staged = installable_skill(runtime, 1, root / 'stage')
            self.assertEqual(staged.name, digest(read_skill_directory(runtime, directory_schema=1)))
            target = root / 'installed'
            install_skill(LocalEnvironment(target), staged, directory_schema=1)
            installed = read_skill_directory(target, directory_schema=1)
            self.assertEqual(installed['scripts/inspect_project.py'], 'print("inspect")\n')
            self.assertIn('Source: pallets/click | sentinel-handling (Sentinel handling)', installed['SKILL.md'])
            self.assertNotIn(unit.unit_id, installed['SKILL.md'])

    def test_own_body_blocks_query_and_original_text(self):
        template = (
            'Implement this click project-evolution milestone as one coherent step. '
            'Preserve existing public behavior except where this milestone intentionally changes it. '
            'Keep the implementation focused and add or preserve regression coverage for the changed behavior.\n\n'
            'Milestone goal:\n- Document and fix command sanitizing with `shlex.split`')
        self.assertEqual(task_query({'prompt': template, 'instance': {'repo': 'pallets/click'}}),
                         'Document and fix command sanitizing with `shlex.split`')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            files = {
                'SKILL.md': (
                    '# Test and diagnose\n\n'
                    'Shared parent procedure that must stay in the core.\n\n'
                    '1. Reproduce the failure before editing.\n\n'
                    '### Shell Command Safety\n\n'
                    '- **Use double quotes for shell arguments:** Always use double quotes.\n\n'
                    '## Reference Files\n\n'
                    '- [references/topic.md](references/topic.md) — Read when editing commands.\n'),
                'references/topic.md': (
                    '# Command patterns\n\n'
                    '## Table of Contents\n\n'
                    '- [Sanitize commands](#sanitize-commands)\n\n'
                    '## Sanitize commands\n\n'
                    'Applies when: command text is sanitized\n'
                    'Use `shlex.split` on the command string before execution.\n\n'
                    '```python\nimport shlex\nshlex.split(command)\n```\n\n'
                    '## Original descriptors\n\n'
                    'Preserve the original file descriptor and override `fileno()`.\n\n'
                    '## Sentinel map\n\n'
                    'Applies when: default_map stores UNSET\n\n'
                    'Preserve UNSET in `default_map`.\n'),
            }
            files['routing/index.yaml'] = build_index(files)
            pool = RepositoryPool(root / 'pool', files)
            pool.store('pallets/click')
            snap, meta = pool.snapshots()[0]
            units = units_from_snapshot(snap, meta)
            self.assertTrue(units)
            self.assertTrue(all('Shared parent procedure' not in unit.markdown for unit in units))
            self.assertFalse(any('Table of Contents' in unit.heading or 'Reference Files' in unit.heading for unit in units))
            self.assertFalse(any(unit.markdown.strip().startswith('- [') for unit in units))
            shlex_unit = next(unit for unit in units if 'shlex.split' in unit.markdown)
            self.assertIn('import shlex', shlex_unit.markdown)
            self.assertNotIn('Preserve UNSET', shlex_unit.markdown)
            shell = next(unit for unit in units if 'double quotes' in unit.markdown)
            self.assertNotIn('Reproduce the failure', shell.markdown)
            self.assertIn('Shared parent procedure', core_markdown(files['SKILL.md']))
            shlex_task = task('pallets/click', 'shlex-case', template)
            found, rows = rank_experiences(shlex_task, pool, recall_k=20)
            self.assertEqual(found[0].unit_id, shlex_unit.unit_id)
            self.assertGreater(len(found), 1)
            self.assertTrue(any('Preserve UNSET' in unit.markdown for unit in found))
            self.assertEqual(rows[0]['source_path'], 'references/topic.md')
            self.assertIn('Sanitize commands', rows[0]['heading'])
            self.assertGreater(rows[0]['bm25_score'], 0)
            narrow, narrow_rows = rank_experiences(shlex_task, pool, recall_k=1)
            self.assertEqual([unit.unit_id for unit in narrow], [shlex_unit.unit_id])
            self.assertTrue(any(row['status'] == 'beyond_recall_k' for row in narrow_rows))
            fd = task('pallets/click', 'fd-case',
                      'Milestone goal:\n- Expose original file descriptor in `CliRunner`')
            fd_found, _fd_rows = rank_experiences(fd, pool, recall_k=20)
            self.assertIn('fileno()', fd_found[0].markdown)
            self.assertTrue(any('shlex.split' in unit.markdown for unit in fd_found))
            unset = task('pallets/click', 'unset-case',
                         'Milestone goal:\n- Reduce blast-radius of `UNSET` in `default_map`')
            unset_found, _unset_rows = rank_experiences(unset, pool, recall_k=20)
            self.assertIn('default_map', unset_found[0].markdown)
            self.assertTrue(any('shlex.split' in unit.markdown for unit in unset_found))
            ctx, runtime, receipt = compose(
                shlex_task, pool.snapshots(), files, [], found, config(), root / 'route',
                counter_factory=lambda _: CharacterCounter(), ranking=rows)
            rendered = (runtime / 'SKILL.md').read_text(encoding='utf-8')
            self.assertEqual(rendered.count('Shared parent procedure'), 1)
            self.assertEqual(ctx.text.count('Shared parent procedure'), 1)
            self.assertIn('shlex.split(command)', rendered)
            self.assertIn('Preserve UNSET', rendered)
            self.assertNotIn('Table of Contents', rendered)
            self.assertEqual(receipt['query'], 'Document and fix command sanitizing with `shlex.split`')
            self.assertEqual(receipt['candidates'][0]['status'], 'selected')
            self.assertEqual(receipt['candidates'][0]['rank'], 1)
            self.assertEqual(receipt['initial_skill_tokens'], CharacterCounter().count(ctx.text))
            hidden = task('pallets/click', 'hide-case',
                          'Milestone goal:\n- Show custom error message in prompt with `hide_input=True`')
            hidden_found = retrieve(hidden, pool, recall_k=20)
            self.assertTrue(any(unit.unit_id == shell.unit_id for unit in hidden_found))

            class FakeModel:
                def __init__(self, _: dict): pass
                def stats(self): return {'calls': 1}

            with patch('rsi.skills.repository_skill.generate_object', return_value={'conflicts': []}):
                conflicts, _ = decide(hidden, hidden_found, config(), root / 'judge', model_factory=FakeModel)
            self.assertEqual(conflicts['excluded'], [])
            self.assertEqual(conflicts['proposals'], [])
            _ctx, weak_runtime, weak_receipt = compose(
                hidden, pool.snapshots(), files, conflicts, hidden_found, config(), root / 'weak',
                counter_factory=lambda _: CharacterCounter())
            weak_text = (weak_runtime / 'SKILL.md').read_text(encoding='utf-8')
            self.assertIn('double quotes', weak_text)
            self.assertNotIn('hide_input', weak_text)
            self.assertNotIn('bool | str', weak_text)
            self.assertEqual(weak_receipt['conflicts'], [])
            self.assertEqual(weak_receipt['curator_excluded_ids'], [])

    def test_different_stages_and_unmatched_symbol_stay_in_the_pool(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pool = RepositoryPool(root / 'pool', seed())
            add_experience(pool, 'pallets/click',
                           '# Sentinel\n## Internal retention\nApplies when: internal default storage\n'
                           'Keep UNSET until the value is consumed.\n'
                           '## Public conversion\nApplies when: public return boundary\n'
                           'Convert UNSET to None at the public return boundary.\n')
            lookup = task('pallets/click', 'lookup-case',
                          'Milestone goal:\n- Hide `Sentinel.UNSET` as `None` in `lookup_default()`')
            found, rows = rank_experiences(lookup, pool, recall_k=20)
            internal = next(unit for unit in found if 'Keep UNSET until' in unit.markdown)
            public = next(unit for unit in found if 'Convert UNSET to None' in unit.markdown)
            self.assertNotIn('lookup_default', internal.markdown + public.markdown)
            self.assertEqual({row['status'] for row in rows if row['unit_id'] in {internal.unit_id, public.unit_id}},
                             {'recalled'})

            class FakeModel:
                def __init__(self, _: dict): pass
                def stats(self): return {'calls': 1}

            with patch('rsi.skills.repository_skill.generate_object', return_value={'conflicts': []}) as gen:
                conflicts, _ = decide(lookup, found, config(), root / 'judge', model_factory=FakeModel)
            self.assertEqual(gen.call_count, 1)
            self.assertEqual(conflicts['excluded'], [])
            ctx, _runtime, receipt = compose(
                lookup, pool.snapshots(), seed(), conflicts, found, config(), root / 'route',
                counter_factory=lambda _: CharacterCounter(), ranking=rows)
            self.assertIn('Keep UNSET until the value is consumed.', ctx.text)
            self.assertIn('Convert UNSET to None at the public return boundary.', ctx.text)
            self.assertEqual(receipt['curator_excluded_ids'], [])
            unresolved = {'conflicts': [{'unit_ids': [internal.unit_id, public.unit_id], 'keep_unit_ids': [],
                                         'reason': 'The notes disagree and do not identify which return this task needs.'}]}
            with patch('rsi.skills.repository_skill.generate_object', return_value=unresolved):
                pending, _ = decide(lookup, found, config(), root / 'pending', model_factory=FakeModel)
            self.assertEqual(pending['excluded'], [])
            self.assertEqual(pending['retained_after_validation'][0]['reason'], 'missing_quotes')
            held, _, held_receipt = compose(
                lookup, pool.snapshots(), seed(), pending, found, config(), root / 'held',
                counter_factory=lambda _: CharacterCounter())
            self.assertIn('Keep UNSET until the value is consumed.', held.text)
            self.assertIn('Convert UNSET to None at the public return boundary.', held.text)
            self.assertEqual(held_receipt['curator_excluded_ids'], [])
            self.assertCountEqual(held_receipt['validation_retained_ids'], [internal.unit_id, public.unit_id])
            self.assertEqual([block['source_unit_ids'] for block in held_receipt['selected']],
                             [[unit.unit_id] for unit in found])

    def test_generic_basis_does_not_drop_either_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pool = RepositoryPool(root / 'pool', seed())
            add_experience(pool, 'pallets/click',
                           '# Prompt\n## Custom label\nApplies when: show_default is a custom label\n'
                           'Display a custom label as (label).\n'
                           '## Actual default\nApplies when: show_default is the real default value\n'
                           'Display an actual default value as [value].\n')
            boilerplate = 'Preserve existing public behavior except where this milestone intentionally changes it.'
            prompt = boilerplate + '\n\nMilestone goal:\n- Show the default string in the prompt.'
            current = task('pallets/click', 'format-case', prompt)
            found = [unit for unit in retrieve(current, pool, recall_k=20)
                     if unit.source_path == 'references/experience.md']
            label = next(unit for unit in found if '(label)' in unit.markdown)
            value = next(unit for unit in found if '[value]' in unit.markdown)
            quoted = {'experience_conflicts': [{
                'unit_ids': [label.unit_id, value.unit_id],
                'quotes': {label.unit_id: 'Display a custom label as (label).',
                           value.unit_id: 'Display an actual default value as [value].'},
                'conflict_point': 'prompt punctuation',
                'same_subject': True, 'same_condition': True, 'same_stage': True,
                'mutually_exclusive': True, 'keep_unit_id': label.unit_id,
                'basis_quote': boilerplate,
                'reason': 'The task says to preserve compatibility.'}], 'task_conflicts': []}
            invented = {'experience_conflicts': [{
                'unit_ids': [label.unit_id, value.unit_id],
                'quotes': {label.unit_id: 'Use brackets for every custom label.',
                           value.unit_id: 'Display an actual default value as [value].'},
                'conflict_point': 'prompt punctuation',
                'same_subject': True, 'same_condition': True, 'same_stage': True,
                'mutually_exclusive': True, 'keep_unit_id': value.unit_id,
                'basis_quote': 'Show the default string in the prompt.',
                'reason': 'The invented quote is not in the experience.'}], 'task_conflicts': []}

            class FakeModel:
                def __init__(self, _: dict): pass
                def stats(self): return {'calls': 1}

            with patch('rsi.skills.repository_skill.generate_object', return_value=quoted):
                generic, _ = decide(current, found, config(), root / 'generic', model_factory=FakeModel)
            self.assertEqual(generic['excluded'], [])
            self.assertEqual(generic['pending'][0]['unit_ids'], [unit.unit_id for unit in found])
            held, _, held_receipt = compose(
                current, pool.snapshots(), seed(), generic, found, config(), root / 'held',
                counter_factory=lambda _: CharacterCounter())
            self.assertIn('(label)', held.text)
            self.assertIn('[value]', held.text)
            self.assertIn('Verify the affected behavior locally', held.text)
            self.assertEqual(held_receipt['curator_excluded_ids'], [])
            with patch('rsi.skills.repository_skill.generate_object', return_value=invented):
                invalid, _ = decide(current, found, config(), root / 'invalid', model_factory=FakeModel)
            self.assertEqual(invalid['excluded'], [])
            self.assertEqual(invalid['pending'], [])
            self.assertEqual(invalid['retained_after_validation'][0]['reason'], 'quotes_not_in_source')
            restored, _, restored_receipt = compose(
                current, pool.snapshots(), seed(), invalid, found, config(), root / 'restored',
                counter_factory=lambda _: CharacterCounter())
            self.assertNotIn('Verify the affected behavior locally', restored.text)
            self.assertEqual([block['source_unit_ids'] for block in restored_receipt['selected']],
                             [[unit.unit_id] for unit in found])

    def test_five_rollouts_share_one_runtime_route(self):
        from rsi.agent.runner import run_group
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / 'task'
            folder.mkdir()
            write_json(folder / 'skill-route.json', {'route_id': 'shared-route'})
            context = SkillContext('shared runtime text', 'context-hash', 12, (),
                                   skill_version='candidate', index_hash='index-hash',
                                   route_id='shared-route')
            runtime = folder / 'task-skill'
            runtime.mkdir()
            seen = []

            def execute(task_arg, index, rollout, skill, config_arg, backend_factory, model_factory, **options):
                seen.append((index, options['skill_context'], options['runtime_skill']))
                return ({'rollout': index, 'status': 'Submitted', 'error': None, 'skill_injected': True,
                         'messages': [{'role': 'assistant', 'content': 'echo ok'}],
                         'artifact': 'diff --git a/a b/a\n',
                         'usage': {'input_tokens': 2, 'output_tokens': 3}}, ScoreHandle())

            group_config = {'execution_mode': 'parallel', 'rollouts': 5, 'evolve': True,
                            'repository_skill': {'enabled': True}, 'skill_access': {'enabled': False}}
            with patch('rsi.agent.runner.execute_one', side_effect=execute):
                report, results, _ = run_group(
                    {'id': 'shared', 'prompt': 'fix', 'benchmark': 'swe'}, folder,
                    type('Skill', (), {'name': 'candidate'})(), group_config,
                    task_route=RouteResult(context, '{}'), runtime_skill=runtime)
            self.assertEqual(len(results), 5)
            self.assertEqual({item[1] for item in seen}, {context})
            self.assertEqual({item[2] for item in seen}, {runtime})
            self.assertEqual(sorted(item[0] for item in seen), list(range(5)))
            self.assertEqual(report['skill_routing']['route_id'], 'shared-route')

    def test_resume_reconciles_commit_without_moving_pointers(self):
        from rsi.agent.repository_runner import run_repository_experiment
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / 'run'
            tasks = [task('pallets/click', 'click-1', 'click sentinel'),
                     task('sqlfluff/sqlfluff', 'sql-1', 'sql whitespace')]
            calls = []

            def route(task_arg, pool, seed_files, skill, config_arg, folder, **kwargs):
                if task_arg['id'] == 'sql-1' and not route.allow_sql:
                    raise RuntimeError('curator unavailable')
                calls.append(('route', task_arg['id']))
                context = SkillContext('runtime', 'h', 1, (), route_id='route-' + task_arg['id'])
                receipt = {'route_id': context.route_id, 'source_skill_versions': {task_repo(task_arg): skill.name},
                           'curator_usage': {'input_tokens': 1, 'output_tokens': 1}}
                write_json(folder / 'skill-route.json', receipt)
                runtime = folder / 'task-skill'
                runtime.mkdir()
                (runtime / 'SKILL.md').write_text('runtime\n', encoding='utf-8')
                return RouteResult(context, '{}'), runtime, receipt

            def group(task_arg, folder, skill, config_arg, **kwargs):
                calls.append(('group', task_arg['id'], kwargs['task_route'].context.route_id, kwargs['runtime_skill']))
                report = {'task_id': task_arg['id'], 'phase': 'test', 'success': True,
                          'input_tokens': 4, 'output_tokens': 5, 'tokens': 9}
                return report, [], ({}, 1)

            saved = experiment_config()
            route.allow_sql = False
            with patch('rsi.agent.repository_runner.route_task', side_effect=route), \
                 patch('rsi.agent.repository_runner.run_group', side_effect=group), \
                 self.assertRaisesRegex(RuntimeError, 'curator unavailable'):
                run_repository_experiment(tasks, saved, output, model_factory=lambda _: None,
                                          backend_factory=lambda *args: None)
            self.assertEqual([item[:2] for item in calls], [('route', 'click-1'), ('group', 'click-1')])
            self.assertEqual(calls[1][2], 'route-click-1')
            self.assertEqual(calls[1][3], output / 'click-1' / 'task-skill')
            failed = pointers(output)
            self.assertEqual(set(failed), {'pallets/click', 'sqlfluff/sqlfluff'})
            with self.assertRaisesRegex(ValueError, 'Uncommitted task folder'):
                run_repository_experiment(tasks, saved, output, resume=True,
                                          model_factory=lambda _: None, backend_factory=lambda *args: None)
            self.assertEqual(pointers(output), failed)
            unfinished = [p for p in output.iterdir() if p.is_dir() and not (p / 'repository-commit.json').is_file()
                          and p.name not in {'initial-skill', 'repository-skills'}]
            self.assertEqual([p.name for p in unfinished], ['sql-1'])
            shutil.rmtree(unfinished[0])
            route.allow_sql = True
            before = pointers(output)
            with patch('rsi.agent.repository_runner.route_task', side_effect=route), \
                 patch('rsi.agent.repository_runner.run_group', side_effect=group):
                reports = run_repository_experiment(tasks, saved, output, resume=True,
                                                    model_factory=lambda _: None, backend_factory=lambda *args: None)
            self.assertEqual([item['task_id'] for item in reports], ['click-1', 'sql-1'])
            self.assertEqual([item[:2] for item in calls],
                             [('route', 'click-1'), ('group', 'click-1'), ('route', 'sql-1'), ('group', 'sql-1')])
            self.assertEqual(pointers(output), before)
            state = read_json(output / 'repository-state.json')
            state['completed'].pop()
            write_json(output / 'repository-state.json', state)
            call_count = len(calls)
            with patch('rsi.agent.repository_runner.route_task', side_effect=route), \
                 patch('rsi.agent.repository_runner.run_group', side_effect=group):
                restored = run_repository_experiment(tasks, saved, output, resume=True,
                                                     model_factory=lambda _: None, backend_factory=lambda *args: None)
            self.assertEqual(len(restored), 2)
            self.assertEqual(len(calls), call_count)
            self.assertEqual(pointers(output), before)
            receipt_path = unfinished_commit(output, 'sql-1')
            receipt = read_json(receipt_path)
            receipt['after_version'] = 'deadbeefdeadbeef'
            write_json(receipt_path, receipt)
            state = read_json(output / 'repository-state.json')
            state['completed'].pop()
            write_json(output / 'repository-state.json', state)
            with self.assertRaisesRegex(ValueError, 'manual reconciliation'):
                run_repository_experiment(tasks, saved, output, resume=True,
                                          model_factory=lambda _: None, backend_factory=lambda *args: None)
            self.assertEqual(pointers(output), before)


class LocalEnvironment:
    """Run only the installer's Python against a disposable substitute for /opt."""
    def __init__(self, root):
        self.root = root

    def execute(self, command, cwd=''):
        if command.startswith('mkdir '):
            self.root.mkdir(parents=True, exist_ok=True)
            return {'returncode': 0, 'output': ''}
        words = shlex.split(command)
        assert words[:2] in (['python', '-c'], ['python3', '-c'])
        script = words[2].replace("'/opt/rsi-skills'", repr(str(self.root)))
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            exec(script, {})
        return {'returncode': 0, 'output': stream.getvalue()}


class ScoreHandle:
    def score(self, artifact):
        return {'success': True}

    def close(self):
        return None


def experiment_config():
    return {'model': {'name': 'test-model'}, 'evolve': False, 'execution_mode': 'parallel', 'rollouts': 5,
            'skill_directory': {'schema': 1}, 'skill_update_method': 'trace2skill',
            'skill_routing': {'enabled': True, 'retrieval_method': 'bm25', 'top_k': 2,
                              'max_initial_skill_tokens': 4000, 'budget_mode': 'strict'},
            'repository_skill': {'enabled': True, 'recall_k': 20, 'max_blocks': 32},
            'skill_access': {'enabled': False}, 'skill_maintenance': {'enabled': False}}


def pointers(output):
    found = {}
    for identity in (output / 'repository-skills').glob('*/repository.json'):
        data = read_json(identity)
        found[data['source_repo']] = read_json(identity.parent / 'store/current.json')['version']
    return found


def unfinished_commit(output, task_id):
    matches = [path for path in output.glob('*/repository-commit.json')
               if read_json(path)['task_id'] == task_id]
    if len(matches) != 1:
        raise AssertionError(f'Expected one commit receipt for {task_id}')
    return matches[0]


if __name__ == '__main__':
    unittest.main()
