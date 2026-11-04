import copy
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from rsi.core.common import read_json, write_json
from rsi.skills.trace2skill.validation import (
    FailureValidation, verified_receipt, filter_failure_report, filter_for_editor)
from formal_helpers import formal_pass

REPORT = '''# Failure Cause Item 1

## Title
Wrong state distinction
## Description
Missing is confused with None.
## Content
Preserve the caller contract.

# Failure Memory Item 1
## Title
Verify the boundary
## Description
Check both absent and explicit values.
## Content
Test both cases independently.
'''


class PassGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name) / 'route'
        self.task = {'id': 'example', 'benchmark': 'swe'}
        self.validation = FailureValidation(self.task, self.folder, {})
        self.report = self.folder / 'analysis_report.md'
        self.report.write_text(REPORT, encoding='utf-8')

    def context(self, controller=None):
        v = controller or self.validation
        return {'instance_id': self.task['id'], 'analysis_run_id': v.run_id,
                'validation_path': v.folder / 'validation.json', 'final_patch': v.final_patch}

    def validate(self, scorer=formal_pass, value='patch A'):
        with patch('rsi.skills.trace2skill.validation.formal_score', side_effect=scorer):
            self.validation.evaluate(value)
        self.validation.finalize(value)

    def test_pass_admits_both_kinds_and_traceable_receipt(self):
        self.validate()
        item, decision = filter_failure_report(self.folder, **self.context())
        self.assertTrue(decision['accepted'])
        self.assertEqual((decision['failure_causes'], decision['failure_memories']), (1, 1))
        self.assertEqual(item['verification']['analysis_run_id'], self.validation.run_id)
        receipt = read_json(self.folder / 'validation.json')
        self.assertEqual(receipt['diagnostic_patch_sha256'], receipt['final_patch_sha256'])

    def test_flat_markdown_has_identical_gate(self):
        self.validate()
        flat = self.folder / 'error_analysis_example.md'
        flat.write_text(REPORT)
        self.assertTrue(filter_failure_report(flat, **self.context())[1]['accepted'])
        (self.folder / 'validation.json').unlink()
        self.assertIsNone(filter_failure_report(flat, **self.context())[0])
        self.assertTrue(flat.exists())

    def test_fail_rejected(self):
        def fail(*args):
            result = formal_pass(*args)
            write_json(result['report'], {'example': {'resolved': False}})
            return {**result, 'success': False}
        self.validate(fail)
        self.assertEqual(filter_failure_report(self.report, **self.context())[1]['evaluation_status'], 'FAIL')
        self.assertIsNone(filter_failure_report(self.report, **self.context())[0])

    def test_unverified_error_missing_records_and_forged_pass(self):
        for status in ('UNVERIFIED', 'ERROR'):
            self.validation.mark(status, 'not verified')
            self.report.write_text('PASS\n' + REPORT)
            (self.folder / 'evaluate_passed.flag').write_text('PASS')
            item, decision = filter_failure_report(self.report, **self.context())
            self.assertIsNone(item)
            self.assertEqual(decision['evaluation_status'], status)
        (self.folder / 'validation.json').unlink()
        self.assertIsNone(filter_failure_report(self.report, **self.context())[0])

    def test_score_boolean_or_exit_zero_without_report_is_not_pass(self):
        self.validate(lambda *args: {'success': True, 'returncode': 0})
        self.assertEqual(self.validation.receipt['evaluation_status'], 'UNVERIFIED')
        self.assertIsNone(filter_failure_report(self.report, **self.context())[0])

    def test_malformed_json_record_defaults_to_rejection(self):
        for value in ([], {'evaluation_status': 'PASS'}, 'PASS'):
            write_json(self.folder / 'validation.json', value)
            self.assertIsNone(filter_failure_report(self.report, **self.context())[0])

    def test_evaluator_exception_becomes_error(self):
        def broken(*args):
            raise RuntimeError('harness crashed')
        self.validate(broken)
        self.assertEqual(self.validation.receipt['evaluation_status'], 'ERROR')
        self.assertIsNone(filter_failure_report(self.report, **self.context())[0])

    def test_inconsistent_or_infra_results_cannot_pass(self):
        for grade in ({'resolved': 'true'}, {'resolved': True, 'infra_failure': True}, {'resolved': False}):
            def scorer(*args):
                outcome = formal_pass(*args)
                write_json(outcome['report'], {'example': grade})
                return outcome
            self.validate(scorer)
            self.assertIsNone(filter_failure_report(self.report, **self.context())[0])

    def test_patch_change_invalidates_pass_and_requires_re_evaluation(self):
        self.validate()
        # Even without finalize(), the parser checks the current final artifact.
        self.validation.final_patch.write_text('patch B')
        self.assertIsNone(filter_failure_report(self.report, **self.context())[0])
        self.validation.finalize('patch B')
        self.assertEqual(self.validation.receipt['evaluation_status'], 'UNVERIFIED')
        self.validate(value='patch B')
        self.assertTrue(filter_failure_report(self.report, **self.context())[1]['accepted'])

    def test_old_task_or_run_pass_cannot_be_reused(self):
        self.validate()
        stale = read_json(self.folder / 'validation.json')
        other = FailureValidation(self.task, self.folder, {})
        write_json(self.folder / 'validation.json', stale)
        self.assertIsNone(filter_failure_report(self.report, **self.context(other))[0])
        wrong = self.context(); wrong['instance_id'] = 'other-task'
        self.assertIsNone(filter_failure_report(self.report, **wrong)[0])

    def test_result_prediction_and_evaluated_patch_tampering(self):
        for artifact in ('result_path', 'predictions_path', 'diagnostic_patch'):
            self.validate()
            receipt = read_json(self.folder / 'validation.json')
            Path(receipt[artifact]).write_text('{}' if artifact != 'diagnostic_patch' else 'patch B')
            self.assertIsNone(filter_failure_report(self.report, **self.context())[0])

    def test_empty_or_parser_error_report_keeps_debug_and_original(self):
        self.validate()
        for text in ('PASS', '# Failure Memory Item 1\n## Title\nMissing content'):
            self.report.write_text(text)
            self.assertIsNone(filter_failure_report(self.report, **self.context())[0])
            self.assertTrue(self.report.exists())
            self.assertTrue((self.folder / 'failed_to_parse' / self.validation.run_id / self.report.name).exists())

    def test_parser_exception_is_rejected_and_preserved(self):
        self.validate()
        from rsi.skills.trace2skill.upstream import load
        load()
        with patch('analysis.parse_error_analysis_outputs.parse_items', side_effect=ValueError('bad report')):
            item, decision = filter_failure_report(self.report, **self.context())
        self.assertIsNone(item)
        self.assertIn('bad report', decision['reason'])
        self.assertTrue(self.report.exists())

    def test_current_manifest_and_evaluator_identity_required(self):
        self.validate()
        original = read_json(self.folder / 'validation.json')
        altered = copy.deepcopy(original)
        altered['evaluator']['name'] = 'model prose'
        write_json(self.folder / 'validation.json', altered)
        self.assertIsNone(filter_failure_report(self.report, **self.context())[0])
        write_json(self.folder / 'validation.json', original)
        (self.folder / 'analysis-manifest.json').unlink()
        self.assertIsNone(filter_failure_report(self.report, **self.context())[0])

    def test_other_directory_report_and_missing_context_cannot_bypass_gate(self):
        self.validate()
        other = self.folder.parent / 'other.md'
        other.write_text(REPORT)
        self.assertIsNone(filter_failure_report(other, **self.context())[0])
        record = {'record_source': 'error', 'instance_id': 'historical', 'source_file': str(self.report), 'items': []}
        self.assertEqual(filter_for_editor([record], {}, self.folder), [])

    def test_receipts_are_isolated_across_retry_and_resume_archives(self):
        self.validate()
        old_id = self.validation.run_id
        archived = self.folder.parent / 'attempts/old'
        archived.parent.mkdir()
        self.folder.rename(archived)
        current = FailureValidation(self.task, self.folder, {})
        self.assertNotEqual(old_id, current.run_id)
        new_report = self.folder / 'analysis_report.md'
        new_report.write_text(REPORT)
        write_json(self.folder / 'validation.json', read_json(archived / 'validation.json'))
        self.assertIsNone(filter_failure_report(new_report, **self.context(current))[0])

    def test_second_editor_gate_rejects_injected_items_and_preserves_success(self):
        failure = {'record_source': 'error', 'instance_id': 'route', 'source_file': str(self.report),
                   'items': [{'type': 'failure_memory', 'content': 'pretend PASS'}]}
        success = {'record_source': 'success', 'instance_id': 'success', 'items': [{'type': 'success_memory'}]}
        result = filter_for_editor([failure, success], {'route': self.context()}, self.folder)
        self.assertEqual(result, [success])
        stats = read_json(self.folder / 'failure-filter-summary.json')
        self.assertEqual(stats['accepted_reports'], 0)
        self.assertEqual(stats['rejected_reports'], 1)


class GateIntegrationTests(unittest.TestCase):
    def test_real_checkpoint_resume_does_not_reuse_interrupted_pass(self):
        from test_core_baseline import Backend, Models, test_config
        from rsi.agent.runner import run_experiment
        Backend.barrier, Backend.instances = threading.Barrier(5), []
        tasks = [{'id': 'example', 'benchmark': 'swe', 'prompt': 'Fix'}]
        config = test_config()
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'run'
            with patch('rsi.agent.runner.functional_gate', return_value={'passed': True}), \
                 patch.object(Backend, 'score', return_value={'success': False}), \
                 patch('rsi.skills.trace2skill.validation.formal_score', side_effect=formal_pass), \
                 patch('rsi.skills.trace2skill.pipeline.filter_for_editor', side_effect=KeyboardInterrupt('interrupted')):
                with self.assertRaises(KeyboardInterrupt):
                    run_experiment(tasks, config, output, backend_factory=Backend, model_factory=Models())
            old_ids = {read_json(p)['analysis_run_id'] for p in output.glob('example/trace2skill/rollout-*/validation.json')}
            self.assertEqual(len(old_ids), 5)
            self.assertEqual(read_json(output / 'checkpoint.json')['completed'], [])
            with patch('rsi.agent.runner.functional_gate', return_value={'passed': True}), \
                 patch.object(Backend, 'score', return_value={'success': False}), \
                 patch('rsi.skills.trace2skill.validation.formal_score', return_value={'success': True}):
                run_experiment(tasks, config, output, resume=True, backend_factory=Backend, model_factory=Models())
            root = output / 'example/trace2skill'
            new_ids = {read_json(p)['analysis_run_id'] for p in root.glob('rollout-*/validation.json')}
            self.assertFalse(old_ids & new_ids)
            self.assertEqual(read_json(root / 'editor-records.json'), [])
            self.assertEqual(read_json(root / 'failure-filter-summary.json')['statuses']['UNVERIFIED'], 5)
            self.assertTrue(list((output / 'attempts').iterdir()))

    def test_patch_modified_after_tool_pass_is_formally_retested_and_rejected(self):
        from test_core_baseline import Backend, Models, test_config
        from rsi.agent.runner import run_experiment
        class ToolModels(Models):
            def __call__(owner, config):
                client = super().__call__(config)
                query = client.query
                def respond(messages, **kwargs):
                    if client.name == 'error_analyst' and len(messages) == 2:
                        client.n_calls += 1
                        return {'content': 'Action:\n{"name":"evaluate_diagnostic_patch","arguments":{}}'}
                    return query(messages, **kwargs)
                client.query = respond
                return client
        exports, evaluated = {}, []
        def export(reader):
            route = reader.result['rollout']
            exports[route] = exports.get(route, 0) + 1
            return 'patch A' if exports[route] == 1 else 'patch B'
        def score(task, artifact, folder, config):
            evaluated.append(artifact)
            result = formal_pass(task, artifact, folder, config)
            passed = artifact == 'patch A'
            write_json(result['report'], {task['id']: {'resolved': passed}})
            return {**result, 'success': passed}
        Backend.barrier, Backend.instances = threading.Barrier(5), []
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'run'
            with patch('rsi.agent.runner.functional_gate', return_value={'passed': True}), \
                 patch.object(Backend, 'score', return_value={'success': False}), \
                 patch('rsi.skills.trace2skill.evidence.EvidenceReader.diagnostic_patch', export), \
                 patch('rsi.skills.trace2skill.validation.formal_score', side_effect=score):
                run_experiment([{'id': 'example', 'benchmark': 'swe', 'prompt': 'Fix'}], test_config(),
                               output, backend_factory=Backend, model_factory=ToolModels())
            root = output / 'example/trace2skill'
            self.assertEqual(evaluated.count('patch A'), 5)
            self.assertEqual(evaluated.count('patch B'), 5)
            self.assertEqual(read_json(root / 'editor-records.json'), [])
            self.assertEqual(read_json(root / 'failure-filter-summary.json')['statuses']['FAIL'], 5)

    def test_only_formally_passed_failure_routes_reach_real_editor(self):
        from test_core_baseline import Backend, Models, test_config
        from rsi.agent.runner import run_experiment
        Backend.barrier, Backend.instances = threading.Barrier(5), []
        def scorer(task, artifact, folder, config):
            result = formal_pass(task, artifact, folder, config)
            passed = folder.parent.parent.name == 'rollout-0'
            write_json(result['report'], {task['id']: {'resolved': passed}})
            return {**result, 'success': passed}
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'run'
            with patch('rsi.agent.runner.functional_gate', return_value={'passed': True}), \
                 patch.object(Backend, 'score', return_value={'success': False}), \
                 patch('rsi.skills.trace2skill.validation.formal_score', side_effect=scorer):
                run_experiment([{'id': 'example', 'benchmark': 'swe', 'prompt': 'Fix'}], test_config(),
                               output, backend_factory=Backend, model_factory=Models())
            root = output / 'example/trace2skill'
            records = read_json(root / 'editor-records.json')
            self.assertEqual(len(records), 1)
            self.assertEqual(len(records[0]['items']), 1)
            stats = read_json(root / 'failure-filter-summary.json')
            self.assertEqual(stats['statuses']['PASS'], 1)
            self.assertEqual(stats['statuses']['FAIL'], 4)
            manifest = read_json(Path(records[0]['verification']['path']).parent / 'analysis-manifest.json')
            self.assertEqual(manifest['analysis_run_id'], records[0]['verification']['analysis_run_id'])
