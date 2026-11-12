import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from rsi.core.common import read_json
from rsi.skills.index import build_index
from rsi.skills.repository_skill import rank_experiences, units_from_snapshot
from rsi.skills.trace2skill.writeback import attach_durable_write, edit_id, repair_markdown, write_patch


class WritebackTests(unittest.TestCase):
    def test_glued_heading_is_repaired_before_the_original_append(self):
        skill = 'Use the helper.\nStale fd references cause crashes during GC cleanup.## Common Fix Pitfalls\n\n- existing\n'
        fenced = skill + '\n```\ncleanup.## Common Fix Pitfalls\n```\n'
        repaired, changed = repair_markdown(fenced)
        self.assertTrue(changed)
        self.assertIn('\n\n## Common Fix Pitfalls\n', repaired)
        self.assertIn('```\ncleanup.## Common Fix Pitfalls\n```', repaired)
        content = '- **Fix consumer-side error handling rather than weakening producer contracts:** Catch ValueError.'
        result = write_patch(
            {'SKILL.md': skill},
            [{'file': 'SKILL.md', 'op': 'append_to_section', 'target_section': '## Common Fix Pitfalls', 'content': content}],
            task_id='evo-79f25adb-001', source_repo='sqlfluff/sqlfluff', repair=None)
        self.assertEqual(result['receipts'][0]['status'], 'applied')
        self.assertIn('\n\n## Common Fix Pitfalls\n', result['files']['SKILL.md'])
        self.assertEqual(result['files']['SKILL.md'].count(content), 1)
        self.assertEqual(result['completion'], 'complete')

    def test_exact_original_patch_is_not_rewritten(self):
        target = '- Close resources—wrapper classes may make close a no-op.'
        skill = f'# Test and diagnose\n\n{target}\n'
        content = '- **Preserve all implicit cleanup behaviors when restructuring context managers:** Keep terminate().'
        translated = []

        def repair(*_args):
            translated.append(True)
            return {'block_id': 'b0000', 'operation': 'insert_after'}

        result = write_patch(
            {'SKILL.md': skill},
            [{'file': 'SKILL.md', 'op': 'insert_after', 'target_section': '# Test and diagnose',
              'target_text': target, 'content': content}],
            task_id='evo-79f25adb-008', source_repo='sqlfluff/sqlfluff', repair=repair)
        self.assertEqual(translated, [])
        self.assertEqual(result['receipts'][0]['status'], 'applied')
        self.assertEqual(result['receipts'][0]['repair_method'], 'exact')
        self.assertIn('—', result['files']['SKILL.md'])
        self.assertNotIn('close a no-op.--', result['files']['SKILL.md'])
        self.assertIn(content, result['files']['SKILL.md'])

    def test_partial_application_keeps_distinct_status_and_changelog(self):
        skill = '# Test and diagnose\n\nKeep this step.\n\n## Common Fix Pitfalls\n\n- old\n'
        added = '- **Match surrounding codebase patterns for warning and error messages:** Reuse the existing wording.'
        result = write_patch(
            {'SKILL.md': skill},
            [{'file': 'SKILL.md', 'op': 'append_to_section', 'target_section': '## Common Fix Pitfalls', 'content': added},
             {'file': 'SKILL.md', 'op': 'replace_in_section', 'target_section': '# Test and diagnose',
              'old_text': 'this sentence is not in the skill', 'content': 'replacement that must not be invented'}],
            task_id='evo-79f25adb-002', source_repo='sqlfluff/sqlfluff', repair=None,
            model_changelog=['Added guidance to catch ValueError'])
        self.assertEqual([item['status'] for item in result['receipts']], ['applied', 'pending_integration'])
        self.assertEqual(result['completion'], 'partial')
        self.assertIn('Applied append_to_section', result['changelog'][0])
        self.assertIn('Pending integration', result['changelog'][1])
        self.assertNotIn('Added guidance to catch ValueError', '\n'.join(result['changelog']))
        self.assertEqual(result['model_changelog'], ['Added guidance to catch ValueError'])
        self.assertNotIn('replacement that must not be invented', result['files']['SKILL.md'])
        self.assertIn(added, result['files']['SKILL.md'])

    def test_reference_fallback_is_retrievable_and_pairs_a_link(self):
        content = '- **Coordinate every layer in output pipelines:** Keep colorama, click.echo, and the three-state color setting aligned.'
        result = write_patch(
            {'SKILL.md': '# Test and diagnose\n\nInspect the repository.\n'},
            [{'file': 'SKILL.md', 'op': 'append_to_section', 'target_section': '## Missing Section', 'content': content}],
            task_id='evo-79f25adb-009', source_repo='sqlfluff/sqlfluff', repair=None)
        receipt = result['receipts'][0]
        self.assertEqual(receipt['status'], 'saved_as_reference')
        self.assertIn(content, result['files'][receipt['saved_path']])
        self.assertIn(f"]({receipt['saved_path']})", result['files']['SKILL.md'])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for path, text in result['files'].items():
                destination = root / path
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(text, encoding='utf-8')
            indexed = result['files'] | {'routing/index.yaml': build_index(result['files'])}
            (root / 'routing').mkdir()
            (root / 'routing/index.yaml').write_text(indexed['routing/index.yaml'], encoding='utf-8')
            meta = {'skill_id': 'sqlfluff', 'version': 'replay', 'source_repo': 'sqlfluff/sqlfluff',
                    'training_run_id': 'replay'}
            units = units_from_snapshot(root, meta)
            self.assertTrue(any(content in unit.markdown for unit in units))
            task = {'id': 'evo-79f25adb-009', 'prompt': 'Coordinate colorama click.echo three-state color',
                    'instance': {'repo': 'sqlfluff/sqlfluff'}}

            class Pool:
                def snapshots(self):
                    return [(root, meta)]

            _recalled, rows = rank_experiences(task, Pool(), recall_k=20)
            self.assertTrue(any(content in row['unit'].markdown for row in rows))

    def test_replay_is_idempotent_and_keeps_edit_ids(self):
        skill = {'SKILL.md': '# Test and diagnose\n\nKeep this step.\n\n## Notes\n\n- old\n'}
        content = '- **Fix consumer-side error handling rather than weakening producer contracts:** Catch ValueError.'
        edit = {'file': 'SKILL.md', 'op': 'append_to_section', 'target_section': '## Notes', 'content': content}
        first = write_patch(skill, [edit], task_id='evo-79f25adb-001', source_repo='sqlfluff/sqlfluff', repair=None)
        second = write_patch(first['files'], [edit], task_id='evo-79f25adb-001', source_repo='sqlfluff/sqlfluff', repair=None)
        self.assertEqual(first['receipts'][0]['edit_id'], second['receipts'][0]['edit_id'])
        self.assertEqual(first['receipts'][0]['edit_id'], edit_id('evo-79f25adb-001', 0, edit))
        self.assertEqual(second['receipts'][0]['status'], 'already_present')
        self.assertEqual(second['files']['SKILL.md'].count(content), 1)
        self.assertEqual(second['completion'], 'no_changes')

    def test_valid_file_without_the_experience_is_not_complete(self):
        skill = '# Test and diagnose\n\nThe file stays valid.\n'
        result = write_patch(
            {'SKILL.md': skill},
            [{'file': 'SKILL.md', 'op': 'replace_in_section', 'target_section': '# Test and diagnose',
              'old_text': 'missing anchor', 'content': 'this experience was not written'}],
            task_id='evo-79f25adb-004', source_repo='sqlfluff/sqlfluff', repair=None,
            model_changelog=['Added the missing experience'])
        self.assertEqual(result['receipts'][0]['status'], 'pending_integration')
        self.assertEqual(result['completion'], 'partial')
        self.assertNotEqual(result['completion'], 'complete')
        self.assertNotIn('this experience was not written', result['files']['SKILL.md'])
        self.assertNotIn('Added the missing experience', '\n'.join(result['changelog']))
        self.assertIn('# Test and diagnose', result['files']['SKILL.md'])

    def test_block_repair_uses_the_indexed_span(self):
        skill = '# Test and diagnose\n\nClose resources—wrapper classes may make close a no-op.\n'
        content = '- **Preserve cleanup:** Call terminate in finally.'

        def repair(_edit, blocks, _reason, _round):
            anchor = next(block for block in blocks if 'wrapper classes' in block['text'])
            return {'block_id': anchor['block_id'], 'operation': 'insert_after'}

        result = write_patch(
            {'SKILL.md': skill},
            [{'file': 'SKILL.md', 'op': 'insert_after', 'target_section': '# Test and diagnose',
              'target_text': 'Close resources--wrapper classes may make close a no-op.', 'content': content}],
            task_id='evo-79f25adb-008', source_repo='sqlfluff/sqlfluff', repair=repair, rounds=1)
        self.assertEqual(result['receipts'][0]['status'], 'applied')
        self.assertEqual(result['receipts'][0]['repair_method'], 'block')
        self.assertIn('—', result['files']['SKILL.md'])
        self.assertIn(content, result['files']['SKILL.md'])

    def test_translation_hook_leaves_an_exact_patch_unchanged(self):
        target = 'Close resources—wrapper classes.'
        patch = SimpleNamespace(
            edits=[SimpleNamespace(file='SKILL.md', op='insert_after', target_section='# Test and diagnose',
                                   target_text=target, content='- Keep terminate().', old_text='', after_section='')],
            changelog_entries=['Translated the dash'], reasoning='model')
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)

            class Evolver:
                patch_pipeline = 'json'
                max_verification_rounds = 1
                client = None

                def run_translation_phase(self, _state, candidate):
                    candidate.edits[0].target_text = candidate.edits[0].target_text.replace('—', '--')
                    return candidate

            evolver = Evolver()
            attach_durable_write(evolver, task={'id': 'evo-79f25adb-008', 'instance': {'repo': 'sqlfluff/sqlfluff'}},
                                 folder=folder)
            returned = evolver.run_translation_phase({}, patch)
            self.assertIn('—', returned.edits[0].target_text)
            _edits, _reason, changelog = evolver.run_apply_phase_programmatic(
                {'SKILL.md': f'# Test and diagnose\n\n{target}\n'}, returned)
            self.assertNotIn('Translated the dash', '\n'.join(changelog))
            stored = read_json(folder / 'experience-records.json')
            self.assertEqual(stored['records'][0]['status'], 'applied')
            self.assertEqual(stored['model_changelog'], ['Translated the dash'])


if __name__ == '__main__':
    unittest.main()
