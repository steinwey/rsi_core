"""Host-owned SWE-bench receipts and the single failure-experience admission gate."""
import hashlib
import json
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from rsi import ROOT
from rsi.core.common import read_json, write_json

STATUSES = ('PASS', 'FAIL', 'UNVERIFIED', 'ERROR')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def formal_score(task, patch, folder, config):
    """Reuse the production harness without starting another solver environment."""
    from rsi.benchmarks.backends import SWEBackend
    backend = SWEBackend.__new__(SWEBackend)
    backend.task, backend.folder = task, folder
    backend.config = dict(config)
    if task.get('evaluation_dataset'):
        backend.config['swe_dataset'] = task['evaluation_dataset']
    return backend.score(patch)


class FailureValidation:
    def __init__(self, task, folder, config):
        self.task, self.folder, self.config = task, Path(folder), config
        self.run_id = uuid.uuid4().hex
        self.manifest = {'instance_id': task['id'], 'analysis_run_id': self.run_id}
        write_json(self.folder / 'analysis-manifest.json', self.manifest)
        self.receipt = None
        self.final_patch = self.folder / 'diagnostic-final.patch'
        self.mark('UNVERIFIED', 'No formal evaluation completed')

    def mark(self, status, reason, patch=None):
        value = {**self.manifest, 'schema': 1, 'evaluation_status': status,
                 'diagnostic_patch': None, 'diagnostic_patch_sha256': None,
                 'final_patch_sha256': None, 'evaluation_time': datetime.now(timezone.utc).isoformat(),
                 'evaluator': {'name': 'SWE-bench', 'version': read_json(ROOT / 'upstreams.lock.json')['SWE-bench']['commit']},
                 'result_path': None, 'result_sha256': None, 'predictions_path': None,
                 'summary': {'reason': reason}}
        if patch is not None:
            self.final_patch.write_text(patch, encoding='utf-8', newline='\n')
            value.update(diagnostic_patch=str(self.final_patch.resolve()),
                         diagnostic_patch_sha256=sha(self.final_patch.read_bytes()),
                         final_patch_sha256=sha(self.final_patch.read_bytes()))
        self.receipt = value
        write_json(self.folder / 'validation.json', value)
        return value

    def evaluate(self, patch):
        # Every attempt has its own prediction/report namespace; no stale report reuse.
        attempt = self.folder / 'evaluations' / uuid.uuid4().hex
        attempt.mkdir(parents=True, exist_ok=False)
        artifact = attempt / 'diagnostic.patch'
        artifact.write_text(patch, encoding='utf-8', newline='\n')
        self.mark('UNVERIFIED', 'Formal evaluation pending', patch)
        receipt = dict(self.receipt)
        receipt['diagnostic_patch'] = str(artifact.resolve())
        try:
            if self.task['benchmark'] not in ('swe', 'swe-evo'):
                raise ValueError('Formal SWE-bench evaluator unavailable for this benchmark')
            outcome = formal_score(self.task, patch, attempt, self.config)
            if outcome.get('error') or outcome.get('infra_failure') or type(outcome.get('success')) is not bool:
                raise RuntimeError(outcome.get('error') or 'Formal evaluator infrastructure failure')
            report = Path(outcome.get('report', '')).resolve()
            predictions = attempt / 'predictions.json'
            if not report.is_file() or not report.is_relative_to(attempt.resolve()):
                receipt['summary'] = {'reason': 'No current formal instance report'}
            else:
                grade = read_json(report).get(self.task['id'], {})
                submitted = read_json(predictions)
                matches = (len(submitted) == 1 and submitted[0].get('instance_id') == self.task['id']
                           and submitted[0].get('model_patch') == patch)
                receipt.update(result_path=str(report), result_sha256=sha(report.read_bytes()),
                               predictions_path=str(predictions.resolve()))
                if grade.get('infra_failure') or outcome.get('infra_failure'):
                    receipt.update(evaluation_status='ERROR', summary={'reason': 'Formal evaluator infrastructure failure'})
                elif (not matches or type(grade.get('resolved')) is not bool
                      or type(outcome.get('success')) is not bool or grade['resolved'] != outcome['success']):
                    receipt['summary'] = {'reason': 'Invalid or inconsistent formal result'}
                else:
                    receipt.update(evaluation_status='PASS' if grade['resolved'] else 'FAIL',
                                   summary={'resolved': grade['resolved'], 'tests_status': grade.get('tests_status', {})})
        except Exception as exc:
            receipt.update(evaluation_status='ERROR', summary={'reason': f'{type(exc).__name__}: {exc}'})
        receipt['evaluation_time'] = datetime.now(timezone.utc).isoformat()
        self.receipt = receipt
        write_json(attempt / 'validation.json', receipt)
        write_json(self.folder / 'validation.json', receipt)
        return receipt

    def finalize(self, patch):
        self.final_patch.write_text(patch, encoding='utf-8', newline='\n')
        current = sha(self.final_patch.read_bytes())
        if self.receipt.get('diagnostic_patch_sha256') != current:
            self.mark('UNVERIFIED', 'Final patch differs from the evaluated patch', patch)
        else:
            self.receipt['final_patch_sha256'] = current
            write_json(self.folder / 'validation.json', self.receipt)


def verified_receipt(path, instance_id, run_id, final_patch):
    """Flags and report prose never participate in this decision."""
    try:
        receipt = read_json(path)
        manifest = read_json(Path(path).parent / 'analysis-manifest.json')
        if manifest.get('instance_id') != instance_id or manifest.get('analysis_run_id') != run_id:
            return 'UNVERIFIED', 'Current analysis manifest mismatch'
        status = receipt.get('evaluation_status', 'UNVERIFIED')
        if status not in STATUSES:
            return 'UNVERIFIED', 'Unknown evaluation status'
        if receipt.get('instance_id') != instance_id or receipt.get('analysis_run_id') != run_id:
            return 'UNVERIFIED', 'Task or analysis run mismatch'
        if status != 'PASS':
            return status, receipt.get('summary', {}).get('reason', 'Formal evaluation did not pass')
        expected = read_json(ROOT / 'upstreams.lock.json')['SWE-bench']['commit']
        if (receipt.get('evaluator') != {'name': 'SWE-bench', 'version': expected}
                or not receipt.get('evaluation_time')):
            return 'UNVERIFIED', 'Missing or mismatched formal evaluator identity'
        patch = Path(receipt['diagnostic_patch'])
        if (Path(final_patch).resolve() != (Path(path).parent / 'diagnostic-final.patch').resolve()
                or patch.resolve().parent.parent != (Path(path).parent / 'evaluations').resolve()):
            return 'UNVERIFIED', 'Patch artifacts belong to another analysis run'
        fingerprint = sha(Path(final_patch).read_bytes())
        if (fingerprint != receipt['diagnostic_patch_sha256'] or fingerprint != receipt['final_patch_sha256']
                or sha(patch.read_bytes()) != fingerprint):
            return 'UNVERIFIED', 'Final or evaluated patch hash mismatch'
        report = Path(receipt['result_path'])
        predictions = Path(receipt['predictions_path'])
        if not report.resolve().is_relative_to(patch.parent.resolve()) or predictions.parent != patch.parent:
            return 'UNVERIFIED', 'Evaluation artifacts belong to another attempt'
        if sha(report.read_bytes()) != receipt['result_sha256']:
            return 'UNVERIFIED', 'Formal result changed'
        rows = read_json(predictions)
        if (len(rows) != 1 or rows[0].get('instance_id') != instance_id
                or sha(rows[0]['model_patch'].encode('utf-8')) != fingerprint):
            return 'UNVERIFIED', 'Submitted prediction does not match patch'
        item = read_json(report).get(instance_id, {})
        if item.get('resolved') is not True or item.get('infra_failure'):
            return 'UNVERIFIED', 'Formal report does not confirm PASS'
        return 'PASS', 'Formal PASS bound to final diagnostic patch'
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError) as exc:
        return 'UNVERIFIED', f'Missing or invalid verification record: {exc}'


def filter_failure_report(report, *, instance_id, analysis_run_id, validation_path, final_patch):
    """Both directory and flat Markdown paths must pass the same receipt gate."""
    report = Path(report)
    if report.is_dir():
        report = report / 'analysis_report.md'
    status, reason = verified_receipt(validation_path, instance_id, analysis_run_id, final_patch)
    decision = {'instance_id': instance_id, 'analysis_run_id': analysis_run_id,
                'report': str(report), 'validation_path': str(validation_path),
                'evaluation_status': status, 'accepted': False, 'reason': reason,
                'failure_causes': 0, 'failure_memories': 0}
    if report.resolve().parent != Path(validation_path).resolve().parent:
        decision.update(evaluation_status='UNVERIFIED', reason='Report belongs to another analysis directory')
        return None, decision
    if status != 'PASS':
        return None, decision
    try:
        from rsi.skills.trace2skill.upstream import load
        load()
        from analysis.parse_error_analysis_outputs import parse_items
        items = parse_items(report.read_text(encoding='utf-8'))
        items = [item for item in items if item.get('title') and item.get('content')]
        if not items:
            raise ValueError('No valid failure experience items')
    except Exception as exc:
        debug = report.parent / 'failed_to_parse' / analysis_run_id
        debug.mkdir(parents=True, exist_ok=True)
        if report.is_file():
            shutil.copy2(report, debug / report.name)
        decision['reason'] = f'Report parse failed: {exc}'
        write_json(debug / 'error.json', decision)
        return None, decision
    decision.update(accepted=True, failure_causes=sum(i['type'] == 'failure_cause' for i in items),
                    failure_memories=sum(i['type'] == 'failure_memory' for i in items))
    return {'record_source': 'error', 'instance_id': f'{instance_id}:{analysis_run_id}',
            'source_file': str(report), 'items': items,
            'verification': {'analysis_run_id': analysis_run_id, 'path': str(validation_path)}}, decision


def filter_for_editor(records, contexts, output):
    """Only admission entry used immediately before the unmodified Skill Editor."""
    accepted, decisions = [], []
    for record in records:
        if record['record_source'] == 'success':
            accepted.append(record)
            continue
        context = contexts.get(record['instance_id'])
        if context is None:
            item, decision = None, {'instance_id': record['instance_id'], 'analysis_run_id': None,
                                    'report': record.get('source_file'), 'evaluation_status': 'UNVERIFIED',
                                    'accepted': False, 'reason': 'Missing current analysis manifest',
                                    'failure_causes': 0, 'failure_memories': 0}
        else:
            item, decision = filter_failure_report(record['source_file'], **context)
        decisions.append(decision)
        if item:
            accepted.append(item)
    summary = {'total_reports': len(decisions),
               'statuses': {s: sum(d['evaluation_status'] == s for d in decisions) for s in STATUSES},
               'accepted_reports': sum(d['accepted'] for d in decisions),
               'rejected_reports': sum(not d['accepted'] for d in decisions),
               'parsed_failure_causes': sum(d['failure_causes'] for d in decisions),
               'parsed_failure_memories': sum(d['failure_memories'] for d in decisions),
               'reports_passed_to_skill_editor': sum(d['accepted'] for d in decisions), 'reports': decisions}
    write_json(Path(output) / 'failure-filter-summary.json', summary)
    lines = ['Failure Analysis Summary', f'Total reports: {len(decisions)}']
    lines.extend(f'{status}: {summary["statuses"][status]}' for status in STATUSES)
    lines.extend(['Experience Filtering', f'Accepted reports: {summary["accepted_reports"]}',
                  f'Rejected reports: {summary["rejected_reports"]}',
                  f'Parsed Failure Causes: {summary["parsed_failure_causes"]}',
                  f'Parsed Failure Memories: {summary["parsed_failure_memories"]}',
                  f'Reports passed to Skill Editor: {summary["reports_passed_to_skill_editor"]}'])
    print('\n'.join(lines), flush=True)
    return accepted
