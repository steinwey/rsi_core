"""Official Trace2Skill ReAct analysis, parallel MAP/REDUCE and deterministic APPLY."""
import hashlib
import asyncio
import shutil
import json
import threading
import time
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path

from rsi.core.common import write_json
from rsi.core.model import Model
from rsi.skills.store import SkillProposalRejected, skill_files, editable
from rsi.skills.documents import validate_consistency
from rsi.skills.directory import read_skill_directory, directory_schema, validate_paths, immutable, INDEX_PATH
from rsi.skills.trace2skill.client import RecordedClient
from rsi.skills.trace2skill.evidence import EvidenceReader
from rsi.skills.trace2skill.prompts import EDITOR_REFERENCE_PLACEMENT, analysis_prompt
from rsi.skills.trace2skill.reports import extract_report
from rsi.skills.trace2skill.upstream import load
from rsi.skills.trace2skill.validation import FailureValidation, filter_for_editor, sha
from rsi.skills.trace2skill.writeback import attach_durable_write


def guard_editor_writes(evolver, work_skill, schema):
    """Guard both initial Apply and verification repairs before any file is written."""
    original_apply = evolver.apply_edits
    def apply_checked(edits):
        edits = list(edits)
        files = read_skill_directory(work_skill, directory_schema=schema)
        for edit in edits:
            path = edit.relative_path
            if (not editable(path) or edit.action not in ('create', 'modify', 'delete')
                    or path == 'SKILL.md' and edit.action == 'delete'
                    or edit.action != 'delete' and not isinstance(edit.content, str)):
                raise SkillProposalRejected(f'Illegal editor FileEdit: {path!r}')
            if edit.action == 'delete':
                files.pop(path, None)
            else:
                files[path] = edit.content
        validate_paths(files)
        original_apply(edits)
    evolver.apply_edits = apply_checked


def attach_reference_placement(evolver):
    """Ask the editor to file topical lessons without editing vendored templates."""
    if getattr(evolver, '_rsi_reference_placement', False):
        return evolver
    instruction = EDITOR_REFERENCE_PLACEMENT.strip()
    for name in ('_map_system_prompt', '_map_patterns_system_prompt', '_merge_system_prompt'):
        current = getattr(evolver, name, None)
        if isinstance(current, str) and instruction not in current:
            setattr(evolver, name, current.rstrip() + '\n\n' + instruction)
    for name in ('_build_map_user_message', '_build_map_user_message_from_patterns',
                 '_build_merge_user_message', '_build_merge_user_message_semantic'):
        original = getattr(evolver, name)

        def wrapped(*args, _original=original, **kwargs):
            text = _original(*args, **kwargs)
            if instruction in text:
                return text
            return text.rstrip() + '\n\n' + instruction

        setattr(evolver, name, wrapped)
    evolver._rsi_reference_placement = True
    return evolver


def evolve_skill(task, results, skill, config, model_factory=Model, *, folder, supervision, prepared_evidence):
    if (supervision.get('schema') != 2 or supervision.get('task_id') != task['id']
            or len(results) not in (1, 5)
            or len(supervision.get('routes', [])) != len(results)
            or {r['rollout'] for r in supervision['routes']} != {r['rollout'] for r in results}
            or any(not r.get('graded') or type(r.get('success')) is not bool for r in supervision['routes'])):
        raise SkillProposalRejected('Trace2Skill requires one or five independent formal grades')
    Agent, AgentConfig, Tool, Parameter, Message, Combined, find_section, apply_edit = load()
    root = Path(folder) / 'trace2skill'
    root.mkdir(parents=True, exist_ok=False)
    settings = config['trace2skill']
    deadline = time.monotonic() + settings['timeout']
    clients = []
    lock = threading.Lock()
    abort_event = threading.Event()
    schema = directory_schema(config)
    original_directory = read_skill_directory(skill, directory_schema=schema)
    original = skill_files(skill)
    state = {k: v for k, v in original.items() if editable(k)}
    evidence, _ = prepared_evidence
    grades = {r['rollout']: r for r in supervision['routes']}
    write_json(root / 'input.json', {'task': task['prompt'], 'supervision': supervision,
                                   'evidence': evidence, 'current_skill': original})

    def client(role, path):
        item = RecordedClient(config[role], path, model_factory, deadline, abort_event=abort_event)
        with lock:
            clients.append((role, item))
        return item

    from analysis.parse_success_analysis_outputs import parse_items as parse_successes
    editor = client('skill_editor_model', root / 'editor')
    validations, contexts = {}, {}

    def analyze(result):
        route = result['rollout']
        path = root / f'rollout-{route}'
        grade = grades[route]
        success = grade['success']
        role = 'skill_success_model' if success else 'skill_error_model'
        model = client(role, path / 'calls')
        reader = EvidenceReader(task, result, grade, path)
        validation = None
        if not success:
            validation = FailureValidation(task, path, config)
            validations[route] = validation
            contexts[f"{task['id']}:rollout-{route}"] = {
                'instance_id': task['id'], 'analysis_run_id': validation.run_id,
                'validation_path': path / 'validation.json', 'final_patch': validation.final_patch}
        from rsi.skills.trace2skill.pathology import trajectory_messages
        data = {'task': task['prompt'], 'trajectory': {'rollout': route, 'messages': trajectory_messages(result), 'artifact': result['artifact'], 'status': result['status']},
                'formal_feedback': grade, 'current_skill': original}
        write_json(path / 'input.json', data)
        try:
            if hashlib.sha256(result['artifact'].encode()).hexdigest() != grade['artifact_sha256']:
                raise SkillProposalRejected('Grading evidence refers to a different patch')
            if success:
                raw = model.chat([Message('system', analysis_prompt(True)),
                                  Message('user', json.dumps(data, ensure_ascii=False))])
            else:
                def evaluate_diagnostic_patch():
                    receipt = validation.evaluate(reader.diagnostic_patch())
                    if receipt['evaluation_status'] == 'ERROR':
                        abort_event.set()
                    return json.dumps({'evaluation_status': receipt['evaluation_status'],
                                       'summary': receipt['summary']}, ensure_ascii=False)
                tools = [Tool('read_evidence', 'Read full archived evidence by offset.', reader.read_evidence,
                              [Parameter('kind', 'string', 'trajectory, patch, grade or command:N'),
                               Parameter('start', 'integer', 'Character offset', False, 0),
                               Parameter('chars', 'integer', 'Up to 12000 characters', False, 10000)]),
                         Tool('evaluate_diagnostic_patch', 'Formally evaluate the current diagnostic patch with SWE-bench. Only this evaluator can establish PASS.',
                              evaluate_diagnostic_patch, [])]
                if task['benchmark'] in ('swe', 'swe-evo'):
                    tools.extend([
                        Tool('read_source', 'Read the disposable patched repository.', reader.read_source,
                             [Parameter('path', 'string', 'Repository relative file'),
                              Parameter('start_line', 'integer', 'Starting line', False, 1),
                              Parameter('lines', 'integer', 'At most 200 lines', False, 100)]),
                        Tool('run_probe', 'Run a Python diagnostic in a network-isolated disposable container.', reader.run_probe,
                             [Parameter('code', 'string', 'Python code, never host code')])])
                agent = Agent(client=model, tools=tools,
                    config=AgentConfig(max_turns=settings['analysis_max_turns'], verbose=False,
                                       system_instructions=analysis_prompt(False)))
                outcome = agent.run(json.dumps(data, ensure_ascii=False))
                write_json(path / 'agent-result.json', asdict(outcome))
                def check_outcome():
                    if abort_event.is_set() or outcome.error not in (None, 'Max turns exceeded'):
                        raise RuntimeError(outcome.error or 'Diagnostic evaluation or model call failed')
                check_outcome()
                # Restore the upstream runner's early-completion continuation.
                # Continue once within the ORIGINAL budget; never reset turns.
                if (validation.receipt['evaluation_status'] != 'PASS'
                        and outcome.total_turns < settings['analysis_max_turns']):
                    outcome = asyncio.run(agent.continue_with_message_async(
                        '[System Check] You have not yet produced a formal PASS evaluation. '
                        'Use evaluate_diagnostic_patch and continue investigating/fixing within the '
                        'remaining turn budget. A completion signal or prose PASS is not verification. '
                        'Then write the complete Failure Cause/Memory Items report and signal '
                        'ACTION: TASK_COMPLETE.'))
                    write_json(path / 'continuation-result.json', asdict(outcome))
                    check_outcome()
                if outcome.error == 'Max turns exceeded':
                    # Same continuation API and budget handling as upstream error_analysis_agent.py.
                    agent.config.max_turns = settings['analysis_max_turns'] + 1
                    try:
                        outcome = asyncio.run(agent.continue_from_last_user_async(
                            '[System Check] Turn budget exhausted. Do NOT call any more tools. '
                            'Based on your investigation so far, write your final analysis '
                            'report now (Failure Cause Items and Failure Memory Items) and '
                            'signal ACTION: TASK_COMPLETE.'))
                    finally:
                        agent.config.max_turns = settings['analysis_max_turns']
                    write_json(path / 'budget-finalization.json', asdict(outcome))
                    check_outcome()
                raw, extraction = extract_report(outcome)
                write_json(path / 'report-extraction.json', extraction)
            (path / 'analysis_report.md').write_text(raw, encoding='utf-8')
            if validation is not None:
                final_patch = reader.diagnostic_patch()
                # A changed patch must receive its own formal evaluation, never an inherited PASS.
                if validation.receipt.get('diagnostic_patch_sha256') != sha(final_patch.encode('utf-8')):
                    validation.evaluate(final_patch)
                validation.finalize(final_patch)
                if validation.receipt['evaluation_status'] == 'ERROR':
                    raise RuntimeError(f"Diagnostic evaluation failed: {validation.receipt['summary']}")
            items = parse_successes(raw) if success else []
            record = {'record_source': 'success' if success else 'error',
                      'instance_id': f"{task['id']}:rollout-{route}",
                      'source_file': str(path / 'analysis_report.md'), 'items': items}
            write_json(path / 'analysis.json', record)
            return record
        finally:
            reader.close()

    started = time.monotonic()
    status = 'failed'
    try:
        records = []
        analysis_failures = []
        # Archive evidence, but never turn a runtime/API failure into no_changes.
        with ThreadPoolExecutor(max_workers=settings['max_workers']) as pool:
            futures = {pool.submit(analyze, result): result['rollout'] for result in results}
            for future in as_completed(futures):
                try:
                    records.append(future.result())
                except Exception as exc:
                    abort_event.set()
                    for pending in futures:
                        pending.cancel()
                    route = futures[future]
                    analysis_failures.append(route)
                    write_json(root / f"rollout-{route}/analysis-error.json",
                               {'error': f'{type(exc).__name__}: {exc}'})
                    if route in validations:
                        validations[route].mark('ERROR', f'{type(exc).__name__}: {exc}')
                        records.append({'record_source': 'error', 'instance_id': f"{task['id']}:rollout-{route}",
                                        'source_file': str(root / f'rollout-{route}/analysis_report.md'), 'items': []})
        records.sort(key=lambda record: record['instance_id'])
        write_json(root / 'analysis-records.json', records)
        if analysis_failures:
            raise RuntimeError(f'Trace2Skill analysis failed for rollout(s) {sorted(analysis_failures)}; task not committed')
        records = filter_for_editor(records, contexts, root)
        write_json(root / 'editor-records.json', records)
        actionable = [record for record in records if record['items']]
        # Official run() mutates its directory; use an isolated working copy, never a stored version.
        work_skill = root / 'skill-work'
        shutil.copytree(skill, work_skill)
        evolver = Combined(client=editor, skill_dir=work_skill, batch_size=1,
            merge_batch_size=settings['merge_batch_size'], max_workers=settings['max_workers'],
            max_merge_levels=settings['max_merge_levels'], temperature=config['skill_editor_model']['temperature'],
            max_tokens=config['skill_editor_model']['max_tokens'], prompt_variant='generic',
            output_dir=root / 'patches', parse_failure_dir=root / 'parse_failures', verbose=False)
        # Reuse the upstream bounded verification/repair loop. Do not add a new
        # model stage or mutate its pinned source to check document consistency.
        guard_editor_writes(evolver._evolver, work_skill, schema)
        upstream_validate = evolver._evolver.validate_skill
        def validate_work_skill():
            passed, message = upstream_validate()
            if passed:
                try:
                    validate_consistency(read_skill_directory(work_skill, directory_schema=schema))
                except ValueError as exc:
                    return False, str(exc)
            return passed, message
        evolver._evolver.validate_skill = validate_work_skill
        attach_durable_write(evolver, task=task, folder=root)
        outcome = evolver.run(actionable)
        if abort_event.is_set():
            raise RuntimeError('Trace2Skill editor/model failed; task not committed')
        updated_directory = read_skill_directory(work_skill, directory_schema=schema)
        if (immutable(updated_directory) != immutable(original_directory)
                or updated_directory.get(INDEX_PATH) != original_directory.get(INDEX_PATH)):
            raise SkillProposalRejected('Editor modified fixed tools or managed metadata')
        updated = {k: v for k, v in updated_directory.items() if editable(k)}
        changed = updated != state
        # The upstream verifier can return normally after exhausting repairs.
        # Confirm its final artifact before allowing an experiment checkpoint.
        from rsi import ROOT
        checked = subprocess.run([sys.executable, '-X', 'utf8',
            str(ROOT / 'skills/skill-creator/scripts/quick_validate.py'), str(work_skill)],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60)
        consistency_error = None
        if checked.returncode == 0 and changed:
            try:
                validate_consistency(read_skill_directory(work_skill, directory_schema=schema))
            except ValueError as exc:
                consistency_error = str(exc)
        validation_result = {'passed': checked.returncode == 0 and not consistency_error,
                             'stdout': checked.stdout, 'stderr': checked.stderr,
                             'consistency_error': consistency_error,
                             'scope': 'document_consistency' if changed else 'document_integrity',
                             'consistency_checked': changed, 'effectiveness_evaluated': False}
        write_json(root / 'final-document-validation.json', validation_result)
        if not validation_result['passed']:
            raise RuntimeError('Trace2Skill final document validation failed; see final-document-validation.json')
        reason, changelog = outcome['reasoning'], outcome['changelog']
        completion = getattr(evolver, 'write_completion', None) or ('complete' if changed else 'no_changes')
        if any(item.get('status') == 'pending_integration' for item in getattr(evolver, 'write_receipts', [])):
            completion = 'partial'
        proposal = {'changes': {k: updated.get(k) for k in state.keys() | updated.keys() if state.get(k) != updated.get(k)},
                    'reason': reason, 'evidence_rollouts': [r['rollout'] for r in results],
                    'changelog': changelog, 'model_changelog': list(getattr(evolver, 'model_changelog', [])),
                    'completion': completion, 'method': 'trace2skill',
                    'validation_scope': 'document_consistency', 'effectiveness_evaluated': False}
        write_json(Path(folder) / 'proposal.json', proposal)
        status = 'completed'
        return proposal
    finally:
        usages = [{'role': role, **item.stats()} for role, item in clients]
        usage = {key: sum(item.get(key, 0) for item in usages)
                 for key in ('calls', 'input_tokens', 'output_tokens', 'reasoning_tokens')}
        costs = [item.get('cost_usd') for item in usages]
        usage['cost_usd'] = sum(costs) if all(cost is not None for cost in costs) else None
        write_json(root / 'usage.json', {'total': usage, 'clients': usages})
        write_json(root / 'summary.json', {'status': status, 'seconds': time.monotonic() - started,
                    'method': 'trace2skill', 'upstream_commit': '3d0b52a140f002a512930252b613c49048f7d5ac',
                    'graded_rollouts': sorted(grades), 'usage': usage})
