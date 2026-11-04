import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from rsi.agent.runner import run_experiment
from rsi.core.common import read_json, write_json
from rsi.skills.documents import consistency_issues, validate_consistency
from rsi.skills.store import SkillStore, SkillProposalRejected, document_gate, load_initial_skill
from rsi.skills.trace2skill.reports import extract_report
from rsi.skills.trace2skill.upstream import load
from test_core_baseline import Backend, Models, test_config
from test_frameworks import BankModels
from formal_helpers import formal_pass

REPORT = '# Failure Memory Item 1\n\n## Title\nBoundary\n## Description\nVerify inputs.\n## Content\nPreserve caller contracts.\n'


class ReportTests(unittest.TestCase):
    def test_completion_marker_before_after_and_between_items_keeps_report(self):
        load()
        from analysis.parse_error_analysis_outputs import parse_items
        for raw, count in ((REPORT + '\nACTION: TASK_COMPLETE', 1),
                           ('ACTION: TASK_COMPLETE\n' + REPORT, 1),
                           (REPORT + '\nACTION: TASK_COMPLETE\n' + REPORT.replace('Item 1', 'Item 2'), 2)):
            with self.subTest(raw=raw[:40]):
                result = SimpleNamespace(final_answer=raw.split('ACTION: TASK_COMPLETE')[0],
                                         steps=[SimpleNamespace(is_final=True, thought=raw)])
                text, metadata = extract_report(result)
                self.assertEqual(len(parse_items(text)), count)
                self.assertNotIn('ACTION: TASK_COMPLETE', text)
                self.assertEqual(metadata['source'], 'final_assistant_response')

    def test_budget_failure_does_not_reuse_an_earlier_final_report(self):
        result = SimpleNamespace(final_answer='Max turns exceeded', steps=[
            SimpleNamespace(is_final=True, thought=REPORT),
            SimpleNamespace(is_final=False, thought='Action: read_source')])
        self.assertEqual(extract_report(result)[0], 'Max turns exceeded')


class DocumentTests(unittest.TestCase):
    def test_missing_and_outside_links_rejected_but_relative_external_and_examples_work(self):
        files = {'SKILL.md': '[contracts](references/contracts.md) [web](https://example.com) [anchor](#use)',
                 'references/contracts.md': '[details](details.md)', 'references/details.md': '# Details'}
        validate_consistency(files)
        files['references/contracts.md'] = '[missing](missing.md)'
        self.assertTrue(consistency_issues(files))
        files['references/contracts.md'] = '[outside](../../private.md)'
        with self.assertRaisesRegex(ValueError, 'outside-Skill'):
            validate_consistency(files)
        files['references/contracts.md'] = '```markdown\n[example](missing.md)\n```'
        validate_consistency(files)

    def test_probe_retesting_conflict_rejected_but_changed_state_exception_allowed(self):
        before_after = 'Run the probe before and after patching.\n'
        ban = 'NEVER run the same probe script more than once.'
        with self.assertRaisesRegex(ValueError, 'Conflicting probe'):
            validate_consistency({'SKILL.md': before_after + ban})
        validate_consistency({'SKILL.md': before_after + ban + ' Except after source changes.'})
        validate_consistency({'SKILL.md': before_after + 'Do not run the same probe script again on unchanged inputs.'})

    def test_legacy_seed_is_readable_but_new_inconsistent_candidate_is_not_promoted(self):
        files, _ = load_initial_skill()
        files['SKILL.md'] += '\nRun probes before and after patching.\nNEVER run the same probe script more than once.\n'
        with tempfile.TemporaryDirectory() as tmp:
            store = SkillStore(Path(tmp) / 'skills', files)
            load_initial_skill(store.current())
            self.assertTrue(document_gate(store.current(), store.fixed_files)['passed'])
            strong = document_gate(store.current(), store.fixed_files, check_consistency=True)
            self.assertFalse(strong['passed'])
            self.assertFalse(strong['effectiveness_evaluated'])
            self.assertEqual(store.propose({})[0], store.current())
            with self.assertRaisesRegex(SkillProposalRejected, 'Conflicting probe'):
                store.propose({'SKILL.md': files['SKILL.md'] + '\nNew advice.\n'})
            with self.assertRaisesRegex(SkillProposalRejected, 'link target'):
                store.propose({'SKILL.md': files['SKILL.md'].split('Run probes')[0] + '[missing](references/missing.md)'})


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / 'run'
        self.task = {'id': 'example', 'benchmark': 'swe', 'prompt': 'Fix caller contract'}
        Backend.barrier, Backend.instances = threading.Barrier(5), []

    def run_task(self, models, *, scorer=formal_pass, successful=False, turns=20):
        config = test_config()
        config['trace2skill']['analysis_max_turns'] = turns
        with patch('rsi.agent.runner.functional_gate', return_value={'passed': True}), \
                patch.object(Backend, 'score', return_value={'success': successful}), \
                patch('rsi.skills.trace2skill.validation.formal_score', side_effect=scorer):
            return run_experiment([self.task], config, self.output, backend_factory=Backend, model_factory=models)

    def test_early_completion_continues_without_resetting_budget(self):
        self.run_task(Models())
        for route in range(5):
            folder = self.output / f'example/trace2skill/rollout-{route}'
            self.assertEqual(read_json(folder / 'agent-result.json')['total_turns'], 1)
            self.assertEqual(read_json(folder / 'continuation-result.json')['total_turns'], 2)
            self.assertEqual(read_json(folder / 'validation.json')['evaluation_status'], 'PASS')
        update = read_json(self.output / 'example/update.json')
        self.assertEqual(update['decision'], 'document_checks_passed')
        self.assertEqual(update['validation_scope'], 'document_consistency')
        self.assertFalse(update['effectiveness_evaluated'])

    def test_existing_tool_pass_does_not_trigger_continuation(self):
        class ToolModels(Models):
            def __call__(owner, config):
                client = super().__call__(config)
                original = client.query
                def query(messages, **kwargs):
                    if client.name == 'error_analyst' and len(messages) == 2:
                        client.n_calls += 1
                        return {'content': 'Action:\n{"name":"evaluate_diagnostic_patch","arguments":{}}'}
                    return original(messages, **kwargs)
                client.query = query
                return client
        self.run_task(ToolModels())
        self.assertFalse(list((self.output / 'example/trace2skill').glob('rollout-*/continuation-result.json')))

    def test_continuation_exhaustion_has_only_one_extra_synthesis_turn(self):
        class ContinueModels(Models):
            def __call__(owner, config):
                client = super().__call__(config)
                original = client.query
                def query(messages, **kwargs):
                    if client.name == 'error_analyst' and '[System Check] You have not yet' in messages[-1]['content']:
                        client.n_calls += 1
                        return {'content': 'Action:\n{"name":"read_evidence","arguments":{"kind":"patch"}}'}
                    return original(messages, **kwargs)
                client.query = query
                return client
        self.run_task(ContinueModels(), turns=2)
        for route in range(5):
            folder = self.output / f'example/trace2skill/rollout-{route}'
            self.assertEqual(read_json(folder / 'continuation-result.json')['total_turns'], 2)
            self.assertEqual(read_json(folder / 'budget-finalization.json')['total_turns'], 3)

    def test_report_after_completion_reaches_the_editor(self):
        class MarkerModels(Models):
            def __call__(owner, config):
                client = super().__call__(config)
                original = client.query
                def query(messages, **kwargs):
                    answer = original(messages, **kwargs)
                    if client.name == 'error_analyst':
                        answer['content'] = 'ACTION: TASK_COMPLETE\n' + answer['content'].replace('ACTION: TASK_COMPLETE', '')
                    return answer
                client.query = query
                return client
        self.run_task(MarkerModels())
        records = read_json(self.output / 'example/trace2skill/editor-records.json')
        self.assertEqual(len(records), 5)
        self.assertTrue(all(len(r['items']) == 1 for r in records))

    def test_formal_infrastructure_failure_is_not_silently_dropped(self):
        with self.assertRaisesRegex(RuntimeError, 'analysis failed'):
            self.run_task(Models(), scorer=lambda *args: {'success': None, 'error': 'harness crashed'})
        self.assertEqual(read_json(self.output / 'checkpoint.json')['completed'], [])
        self.assertFalse((self.output / 'example/trace2skill/editor-records.json').exists())

    def test_editor_api_error_cannot_become_a_no_changes_commit(self):
        class BrokenEditor(Models):
            def __call__(owner, config):
                client = super().__call__(config)
                if client.name == 'judge':
                    def fail(*args, **kwargs):
                        raise RuntimeError('editor API unavailable')
                    client.query = fail
                return client
        with self.assertRaisesRegex(RuntimeError, 'editor/model failed'):
            self.run_task(BrokenEditor(), successful=True)
        state = read_json(self.output / 'checkpoint.json')
        self.assertEqual(state['completed'], [])
        self.assertEqual(read_json(self.output / 'skills/current.json')['version'], state['initial_skill_version'])
        self.assertEqual(read_json(self.output / 'example/update.json')['decision'], 'update_error')

    def test_consistency_error_uses_existing_bank_repair_loop(self):
        def conflicting(answer):
            return {**answer, 'skill': answer['skill'] +
                    '\nRun probes before and after patching.\nNEVER run the same probe script more than once.\n'}
        models = BankModels(replies=[conflicting])
        config = test_config()
        config['skill_update_method'] = 'reasoningbank'
        config['skill_editor_model']['name'] = 'bank'
        with patch('rsi.agent.runner.functional_gate', return_value={'passed': True}):
            run_experiment([self.task], config, self.output, backend_factory=Backend, model_factory=models)
        checks = read_json(self.output / 'example/reasoningbank/validation.json')['attempts']
        self.assertEqual([c['passed'] for c in checks], [False, True])
        self.assertIn('Conflicting probe', models.aggregations[1]['validation_error'])
        self.assertTrue(read_json(self.output / 'example/update.json')['accepted'])
        self.assertEqual(len(models.aggregations), 2)

    def test_consistency_error_uses_bounded_upstream_repairs_then_blocks_commit(self):
        class InconsistentEditor(Models):
            def __call__(owner, config):
                client = super().__call__(config)
                original = client.query
                def query(messages, **kwargs):
                    answer = original(messages, **kwargs)
                    if client.name == 'judge':
                        value = json.loads(answer['content'].split('\n', 1)[1].rsplit('\n', 1)[0])
                        value['edits'][0]['content'] = ('Run probes before and after patching.\n'
                                                       'NEVER run the same probe script more than once.')
                        answer['content'] = '```json\n' + json.dumps(value) + '\n```'
                    return answer
                client.query = query
                return client
        models = InconsistentEditor()
        with self.assertRaisesRegex(RuntimeError, 'final document validation failed'):
            self.run_task(models, successful=True)
        repair_requests = [messages for name, messages in models.requests if name == 'judge'
                           and 'Conflicting probe instructions:' in json.dumps(messages)]
        self.assertEqual(len(repair_requests), 3)
        self.assertEqual(read_json(self.output / 'checkpoint.json')['completed'], [])
        final = read_json(self.output / 'example/trace2skill/final-document-validation.json')
        self.assertFalse(final['passed'])
        self.assertIn('Conflicting probe', final['consistency_error'])


if __name__ == '__main__':
    unittest.main()
