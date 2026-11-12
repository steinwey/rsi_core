"""Deterministic idle-loop pathology scan. No model calls and no runtime hook."""
import hashlib
import json
import re
from pathlib import Path


def normalize_command(command):
    """Collapse whitespace so wrapped copies of one command compare equal."""
    return ' '.join((command or '').split())


# Calibration landing point for a future runtime. detect() does not read this table.
# H means: on the H-th repeat, skip that command's sandbox execution and return a
# rejection observation. The rollout itself continues. Unknown models stay in shadow.
HARD_STOP_DEFAULTS = {
    'qwen3.8-flash': 20,       # 修正谓词后成功轨迹最长链 17（805ad7/010/rollout-1 awk 重读），留余量
    'deepseek-v4.1-flash': 8,  # 成功轨迹最长链仅 3（ablation-g2-rep2/005）
}
HARD_STOP_FALLBACK = None  # 未知模型默认不硬停（shadow 模式先校准）


def default_is_edit(command):
    """File writes, patch applies, in-place sed, and shell redirection into a file.

    Python text inside the command counts too, including a `python - <<'EOF'` body.
    Matching is textual and does not execute anything. `2>&1`, heredoc openers (`<<`),
    comparison (`>=`), and arrows (`->`, `=>`) are not writes. Callers can replace
    this predicate.
    """
    text = command or ''
    if re.search(r'(?i)\bsed\b[^\n;&|]*\s-i(\b|$)', text):
        return True
    if re.search(r'(?i)\bgit\s+apply\b|\bapply_patch\b|\bpatch\s+-', text):
        return True
    if re.search(r'(?i)(?:^|[\s;&|])tee\b', text):
        return True
    if 'write_text(' in text or 'write_bytes(' in text:
        return True
    if _python_write(text):
        return True
    return _has_file_redirect(text)


def _python_write(text):
    """open(..., 'w'/'a'/'x') followed by .write(, Path().write_*, shutil copy/move, os rename/replace."""
    for match in re.finditer(r'''(?is)open\s*\([^)]*['"](?:w|a|x)(?:\+|b|b\+)?['"]''', text):
        if '.write(' in text[match.end():]:
            return True
    if re.search(r'(?is)\bPath\s*\([\s\S]*?\)\s*\.write_(?:text|bytes)\s*\(', text):
        return True
    if re.search(r'(?i)\bshutil\s*\.\s*(?:copyfile|copy|move)\s*\(', text):
        return True
    if re.search(r'(?i)\bos\s*\.\s*(?:rename|replace)\s*\(', text):
        return True
    return False


def _has_file_redirect(text):
    index = 0
    while index < len(text):
        if text.startswith('<<', index):
            index += 2
            continue
        if text.startswith('>>', index):
            previous = text[index - 1] if index else ''
            following = text[index + 2] if index + 2 < len(text) else ''
            if previous != '&' and following != '&':
                return True
            index += 2
            continue
        if text[index] == '>':
            previous = text[index - 1] if index else ''
            following = text[index + 1] if index + 1 < len(text) else ''
            if previous not in '-&=' and following not in '=&':
                return True
        index += 1
    return False


def output_sha(step):
    """Use a recorded sha, otherwise hash the output text. Missing output hashes empty."""
    recorded = step.get('output_sha256')
    if recorded:
        return recorded
    output = step.get('output')
    if output is None:
        output = ''
    if not isinstance(output, str):
        output = json.dumps(output, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(output.encode('utf-8')).hexdigest()


def detect(steps, *, k1=3, k2=3, k3=3, step_limit=None, is_edit=None):
    """Return pathology findings for one rollout step sequence.

    Each step may carry command, output or output_sha256, truncated, format_failed, and step.
    """
    if is_edit is None:
        is_edit = default_is_edit
    prepared = [_prepare(step, index, is_edit) for index, step in enumerate(steps)]
    findings = []
    findings.extend(_repeated_commands(prepared, k1))
    findings.extend(_consecutive(prepared, 'truncated', 'truncation_loop', k2))
    findings.extend(_consecutive(prepared, 'format_failed', 'format_death', k3))
    stall = _zero_edit_stall(prepared, step_limit)
    if stall:
        findings.append(stall)
    return findings


def _prepare(step, index, is_edit):
    command = step.get('command')
    number = step.get('step')
    if type(number) is not int:
        number = index + 1
    text = command if isinstance(command, str) and command.strip() else None
    return {'step': number, 'command': text, 'normalized': normalize_command(text) if text else None,
            'sha': output_sha(step) if text else None, 'edit': bool(text and is_edit(text)),
            'truncated': bool(step.get('truncated')), 'format_failed': bool(step.get('format_failed')) and not step.get('truncated'),
            'error_type': step.get('error_type')}


def _repeated_commands(steps, k1):
    grouped = {}
    for item in steps:
        if item['normalized']:
            grouped.setdefault(item['normalized'], []).append(item)
    findings = []
    for occurrences in grouped.values():
        chain = []
        previous = None
        for item in occurrences:
            blocked = previous is not None and _edit_between(steps, previous['step'], item['step'])
            same = chain and item['sha'] == chain[0]['sha'] and not blocked
            if same:
                chain.append(item)
            else:
                findings.extend(_emit_repeat(chain, k1))
                chain = [item]
            previous = item
        findings.extend(_emit_repeat(chain, k1))
    return findings


def _edit_between(steps, start, end):
    return any(item['edit'] and start < item['step'] < end for item in steps)


def _emit_repeat(chain, k1):
    if len(chain) < k1:
        return []
    command = chain[0]['command']
    return [_finding('repeated_command', chain[0]['step'], chain[-1]['step'], len(chain), k1, {
        'command': command[:500], 'count': len(chain), 'output_sha256': chain[0]['sha']})]


def _consecutive(steps, flag, kind, threshold):
    findings = []
    run = []
    for item in steps:
        if item[flag]:
            run.append(item)
            continue
        findings.extend(_emit_run(run, kind, threshold))
        run = []
    findings.extend(_emit_run(run, kind, threshold))
    return findings


def _emit_run(run, kind, threshold):
    if len(run) < threshold:
        return []
    evidence = {'count': len(run)}
    if kind == 'format_death':
        evidence['error_types'] = list(dict.fromkeys(item['error_type'] for item in run if item['error_type']))
    return [_finding(kind, run[0]['step'], run[-1]['step'], len(run), threshold, evidence)]


def _zero_edit_stall(steps, step_limit):
    if type(step_limit) is not int or step_limit <= 0 or not steps:
        return None
    if len(steps) * 2 <= step_limit or any(item['edit'] for item in steps):
        return None
    return _finding('zero_edit_stall', steps[0]['step'], steps[-1]['step'], len(steps), step_limit, {
        'steps': len(steps), 'step_limit': step_limit, 'edit_count': 0})


def _finding(kind, start, end, count, threshold, evidence):
    high = count >= max(threshold * 2, threshold + 1) if kind != 'zero_edit_stall' else count >= threshold
    return {'type': kind, 'start_step': start, 'end_step': end, 'severity': 'high' if high else 'medium',
            'evidence': evidence}


def load_rollout_steps(rollout):
    """Read one rollout's protocol and command index. Does not write."""
    folder = Path(rollout)
    protocol = _jsonl(folder / 'action-protocol.jsonl')
    commands = _jsonl(folder / 'commands' / 'index.jsonl')
    if not commands and (folder / 'commands').is_dir():
        commands = [_read_json(path) for path in sorted((folder / 'commands').glob('[0-9]*.json'))]
    if protocol:
        return _steps_from_protocol(protocol, commands)
    return [_command_step(row, row.get('sequence') or index + 1) for index, row in enumerate(commands)]


def _steps_from_protocol(protocol, commands):
    pending = {}
    for row in commands:
        pending.setdefault(row.get('assistant_message'), []).append(row)
    steps = []
    for index, event in enumerate(protocol):
        command = sha = None
        bucket = pending.get(event.get('assistant_message')) if event.get('executed') else None
        if bucket:
            row = bucket.pop(0)
            command, sha = row.get('command'), _recorded_sha(row)
        error = event.get('error_type')
        truncated = error == 'output_truncated' or event.get('finish_reason') == 'length'
        steps.append({'step': event.get('model_call') if type(event.get('model_call')) is int else index + 1,
                      'command': command, 'output_sha256': sha, 'truncated': truncated,
                      'format_failed': bool(error) and not truncated, 'error_type': error})
    leftovers = [row for rows in pending.values() for row in rows]
    leftovers.sort(key=lambda row: row.get('sequence') or 0)
    steps.extend(_command_step(row, row.get('sequence') or len(steps) + offset + 1)
                 for offset, row in enumerate(leftovers))
    return steps


def _command_step(row, number):
    return {'step': number, 'command': row.get('command'), 'output_sha256': _recorded_sha(row),
            'truncated': False, 'format_failed': False, 'error_type': None}


def _recorded_sha(row):
    output = row.get('output')
    if isinstance(output, dict):
        return output.get('sha256')
    return None


def _jsonl(path):
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]


def _read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))
