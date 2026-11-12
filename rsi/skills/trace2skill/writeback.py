"""Durable Trace2Skill writes. Upstream apply stays hash-locked and is only called."""
import hashlib
import json
import re
from types import SimpleNamespace

from rsi.core.common import parse_object
from rsi.skills.directory import editable

_FENCE = re.compile(r'^( {0,3})(`{3,}|~{3,})(.*)$')
_GLUED = re.compile(r'([^\s#`])(#{1,6}[ \t]+\S[^\n]*)')
_REFERENCE = re.compile(r'references/[A-Za-z0-9_][A-Za-z0-9_.-]*\.md')
_FIELDS = ('file', 'op', 'target_section', 'target_text', 'content', 'old_text', 'after_section')
_ADDITIVE = {'append_to_section', 'insert_after', 'insert_before', 'add_section', 'create'}
_REMOVALS = {'delete_section', 'delete_file'}
REPAIR_INSTRUCTION = """You locate one failed skill edit. Return only a JSON object with keys block_id and operation.
operation is one of insert_after, insert_before, replace, delete, append_to_section.
Choose the single block whose text is the edit's anchor. Do not copy or rewrite the skill text.
If no block is the anchor, return {"block_id": null, "operation": null}."""


def edit_id(task_id, index, edit):
    payload = {'task_id': task_id, 'index': index}
    payload.update({name: _value(edit, name) for name in _FIELDS})
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return 'exp-' + digest[:24]


def repair_markdown(text):
    """Split a heading that was glued to the previous paragraph. Fenced code stays byte-for-byte."""
    front, body = _frontmatter(text)
    lines = body.split('\n')
    repaired, fence, changed = [], None, False
    for line in lines:
        marker = _FENCE.match(line)
        if fence:
            repaired.append(line)
            if (marker and marker.group(2)[0] == fence[0] and len(marker.group(2)) >= fence[1]
                    and not marker.group(3).strip()):
                fence = None
            continue
        if marker:
            fence = (marker.group(2)[0], len(marker.group(2)))
            repaired.append(line)
            continue
        updated = _GLUED.sub(r'\1\n\n\2', line)
        changed = changed or updated != line
        repaired.extend(updated.split('\n'))
    return front + '\n'.join(repaired), changed


def write_patch(files, edits, *, task_id, source_repo='', repair=None, rounds=3,
                model_changelog=None):
    """Apply original edits, then repair only the misses. Records stay complete either way."""
    updated = dict(files)
    structure = []
    for path, text in list(updated.items()):
        if not _markdown(path) or not isinstance(text, str):
            continue
        if not edits:
            continue
        revised, changed = repair_markdown(text)
        if changed:
            updated[path] = revised
            structure.append(path)
    prepared = [_prepared(task_id, index, edit, source_repo) for index, edit in enumerate(edits)]
    order = _groups(prepared)
    receipts = []
    for indexes in order:
        receipts.extend(_apply_group(updated, [prepared[index] for index in indexes], repair, rounds))
    for path in list(updated):
        if _markdown(path) and isinstance(updated[path], str):
            updated[path], _ = repair_markdown(updated[path])
    receipts = sorted((_audit(updated, receipt) for receipt in receipts), key=lambda item: item['index'])
    changed = any(updated.get(path) != files.get(path) for path in set(files) | set(updated))
    return {'files': updated, 'receipts': receipts, 'structure_repaired': structure,
            'completion': completion_of(receipts, changed),
            'changelog': _changelog(receipts),
            'model_changelog': list(model_changelog or [])}


def completion_of(receipts, changed):
    if any(item['status'] == 'pending_integration' for item in receipts):
        return 'partial'
    if not receipts:
        return 'no_changes' if not changed else 'complete'
    if all(item['status'] == 'already_present' for item in receipts) and not changed:
        return 'no_changes'
    return 'complete'


def attach_durable_write(evolver, *, task, folder):
    """Keep MAP/REDUCE, but do not let translation rewrite an edit before the original is tried."""
    from rsi.skills.trace2skill.upstream import load
    message_type = load()[4]
    instance = task.get('instance') if isinstance(task.get('instance'), dict) else {}
    source_repo = instance.get('repo') or task.get('repo') or ''
    evolver.write_receipts = []
    evolver.model_changelog = []
    evolver.write_completion = None

    def translate(skill_state, patch):
        if evolver.patch_pipeline != 'json':
            return evolver.__class__.run_translation_phase(evolver, skill_state, patch)
        return patch

    def apply(skill_state, patch):
        result = write_patch(
            skill_state, list(patch.edits), task_id=task['id'], source_repo=source_repo,
            repair=lambda edit, blocks, reason, round_index: _editor_repair(
                evolver, message_type, edit, blocks, reason, round_index),
            rounds=evolver.max_verification_rounds,
            model_changelog=list(patch.changelog_entries))
        evolver.write_receipts.extend(result['receipts'])
        evolver.model_changelog.extend(result['model_changelog'])
        evolver.write_completion = 'partial' if (
            evolver.write_completion == 'partial' or result['completion'] == 'partial') else result['completion']
        _write_records(folder, evolver.write_receipts, evolver.model_changelog, evolver.write_completion)
        from skill_evolver.skill_evolving_agent import FileEdit
        originals = skill_state
        produced = result['files']
        file_edits = []
        for path in sorted(set(originals) | set(produced)):
            if produced.get(path) == originals.get(path):
                continue
            if path not in produced:
                file_edits.append(FileEdit(path, '', 'delete'))
            else:
                file_edits.append(FileEdit(path, produced[path], 'modify' if path in originals else 'create'))
        return file_edits, patch.reasoning, result['changelog']

    evolver.run_translation_phase = translate
    evolver.run_apply_phase_programmatic = apply
    return evolver


def _editor_repair(evolver, message_type, edit, blocks, reason, round_index):
    payload = {'round': round_index, 'reason': reason, 'edit': _public_edit(edit),
               'blocks': [{'block_id': block['block_id'], 'heading': block['heading'],
                           'text': block['text'][:500]} for block in blocks]}
    try:
        reply = evolver.client.chat([
            message_type('system', REPAIR_INSTRUCTION),
            message_type('user', json.dumps(payload, ensure_ascii=False))])
        return parse_object(reply)
    except Exception:
        abort = getattr(evolver.client, 'abort_event', None)
        if abort is not None:
            abort.clear()
        return None


def _write_records(folder, receipts, model_changelog, completion):
    from rsi.core.common import write_json
    write_json(folder / 'experience-records.json', {
        'completion': completion, 'model_changelog': model_changelog, 'records': receipts})


def _prepared(task_id, index, edit, source_repo):
    item = as_edit(edit)
    identity = edit_id(task_id, index, item)
    return {'id': identity, 'index': index, 'task_id': task_id, 'source_repo': source_repo, 'edit': item}


def _groups(prepared):
    pending = set(range(len(prepared)))
    groups = []
    for index, item in enumerate(prepared):
        if index not in pending:
            continue
        paths = _referenced(item['edit'])
        members = [index]
        pending.remove(index)
        if paths:
            for other in range(len(prepared)):
                if other in pending and paths & _referenced(prepared[other]['edit']):
                    members.append(other)
                    pending.remove(other)
        groups.append(sorted(members))
    return groups


def _referenced(edit):
    found = set(_REFERENCE.findall(edit.content))
    if edit.op == 'create':
        found.add(edit.file)
    return {path for path in found if path.startswith('references/')}


def _apply_group(files, members, repair, rounds):
    created = set()
    receipts = []
    for member in members:
        if member['edit'].op == 'create':
            receipt = _apply_one(files, member, repair, rounds)
            if receipt['status'] in ('applied', 'already_present', 'saved_as_reference'):
                created.add(member['edit'].file)
            receipts.append(receipt)
    for member in members:
        if member['edit'].op == 'create':
            continue
        linked = _referenced(member['edit']) & created
        if linked and not _satisfied(files.get(member['edit'].file, ''), member['edit']):
            located = _attempt(files, member['edit'])
            if located[0] in ('applied', 'already_present'):
                files[member['edit'].file] = located[1]
                receipts.append(_receipt(member, located[0], located[2] not in ('exact', 'none'), located[2], ''))
                continue
            for path in sorted(linked):
                applies = _applies(member['edit'].content) or _applies(files.get(path, ''))
                files['SKILL.md'], added = _ensure_link(files.get('SKILL.md', ''), path, applies)
            receipts.append(_receipt(
                member, 'applied', True, 'reference_link', '',
                final_target={'file': 'SKILL.md', 'paths': sorted(linked)}))
            continue
        receipts.append(_apply_one(files, member, repair, rounds))
    return receipts


def _apply_one(files, member, repair, rounds):
    edit = member['edit']
    if not _markdown(edit.file) and edit.op not in ('create', 'delete_file'):
        return _receipt(member, 'pending_integration', False, 'none', 'unsupported_file')
    status, text, method = _attempt(files, edit)
    if status != 'miss':
        if edit.op == 'delete_file':
            files.pop(edit.file, None)
        elif edit.op != 'delete_file':
            files[edit.file] = text
        return _receipt(member, status, method not in ('exact', 'none'), method, '')
    reason = 'not_found'
    for round_index in range(max(0, rounds)):
        if repair is None:
            break
        blocks = _blocks(files.get(edit.file, ''), edit.file)
        choice = repair(edit, blocks, reason, round_index)
        if not isinstance(choice, dict):
            reason = 'repair_unavailable'
            continue
        placed = _apply_block(files.get(edit.file, ''), edit, blocks, choice)
        if placed is None:
            reason = 'repair_rejected'
            continue
        if _verified(placed, edit):
            files[edit.file] = placed
            return _receipt(member, 'applied', True, 'block', '', final_target={
                'file': edit.file, 'block_id': choice.get('block_id'), 'operation': choice.get('operation')})
        reason = 'repair_not_effective'
    if edit.op in _ADDITIVE:
        path = _save_reference(files, member)
        return _receipt(member, 'saved_as_reference', True, 'reference', '', saved_path=path,
                        final_target={'file': path})
    return _receipt(member, 'pending_integration', False, 'none', reason)


def _attempt(files, edit):
    if edit.op == 'delete_file':
        if edit.file not in files:
            return 'already_present', '', 'none'
        return 'applied', '', 'exact'
    text = files.get(edit.file, '')
    if edit.op == 'create':
        if edit.file in files and (_norm(files[edit.file]) == _norm(edit.content) or _contains(files[edit.file], edit.content)):
            return 'already_present', files[edit.file], 'none'
        if edit.file in files:
            return 'miss', text, ''
        body = edit.content if edit.content.endswith('\n') else edit.content + '\n'
        return 'applied', body, 'exact'
    if _satisfied(text, edit):
        return 'already_present', text, 'none'
    find_bounds, apply_edit = _upstream()
    revised = apply_edit(text, edit)
    if revised != text and _verified(revised, edit):
        return 'applied', revised, 'exact'
    revised = _normalized_apply(text, edit, find_bounds)
    if revised is not None and revised != text and _verified(revised, edit):
        return 'applied', revised, 'normalized'
    return 'miss', text, ''


def _satisfied(text, edit):
    if edit.op in _ADDITIVE and edit.op != 'create':
        return _contains(text, edit.content)
    if edit.op == 'replace_in_section':
        return bool(edit.content.strip()) and _contains(text, edit.content)
    if edit.op == 'delete_section':
        return _section_span(text, edit.target_section) is None and bool(edit.target_section.strip())
    return False


def _verified(text, edit):
    if edit.op in _REMOVALS:
        if edit.op == 'delete_file':
            return True
        return _section_span(text, edit.target_section) is None
    if edit.op == 'replace_in_section':
        return _contains(text, edit.content)
    if edit.op in _ADDITIVE:
        return _contains(text, edit.content)
    return False


def _normalized_apply(text, edit, find_bounds):
    if edit.op == 'append_to_section':
        span = _section_span(text, edit.target_section, find_bounds)
        if span is None:
            return None
        return _insert_at(text, _section_insert(text, span), edit.content)
    if edit.op == 'add_section':
        header = edit.target_section or '## New Section'
        if _section_span(text, header, find_bounds) is not None and _contains(text, edit.content):
            return text
        anchor = _section_span(text, edit.after_section, find_bounds) if edit.after_section else None
        point = anchor[1] if anchor else len(text)
        block = f'\n\n{header}\n\n{edit.content.strip()}\n'
        return text[:point].rstrip('\n') + block + text[point:]
    if edit.op == 'delete_section':
        span = _section_span(text, edit.target_section, find_bounds)
        if span is None:
            return None
        return text[:span[0]].rstrip('\n') + '\n' + text[span[1]:].lstrip('\n')
    needle = edit.old_text if edit.op == 'replace_in_section' else edit.target_text
    if not needle.strip():
        return None
    window = _section_span(text, edit.target_section, find_bounds) if edit.target_section else (0, len(text))
    if window is None:
        return None
    span = _unique_span(text[window[0]:window[1]], needle)
    if span is None:
        return None
    start, end = window[0] + span[0], window[0] + span[1]
    if edit.op == 'replace_in_section':
        return text[:start] + edit.content + text[end:]
    block = '\n' + edit.content.strip('\n') + '\n'
    if edit.op == 'insert_after':
        return text[:end] + block + text[end:]
    if edit.op == 'insert_before':
        return text[:start] + block + text[start:]
    return None


def _apply_block(text, edit, blocks, choice):
    operation = choice.get('operation')
    block_id = choice.get('block_id')
    if operation not in ('insert_after', 'insert_before', 'replace', 'delete', 'append_to_section'):
        return None
    block = next((item for item in blocks if item['block_id'] == block_id), None)
    if block is None or text[block['start']:block['end']] != block['text']:
        return None
    content = edit.content.strip('\n')
    if operation == 'append_to_section':
        header = block['text'].strip()
        span = _section_span(text, header)
        if span is None:
            return None
        return _insert_at(text, _section_insert(text, span), edit.content)
    if operation == 'insert_after':
        return text[:block['end']] + '\n' + content + '\n' + text[block['end']:]
    if operation == 'insert_before':
        return text[:block['start']] + content + '\n' + text[block['start']:]
    if operation == 'replace':
        return text[:block['start']] + edit.content + text[block['end']:]
    return text[:block['start']] + text[block['end']:]


def _save_reference(files, member):
    edit = member['edit']
    path = f"references/{member['id']}.md"
    applies = _applies(edit.content)
    if path not in files or not _contains(files[path], edit.content):
        files[path] = (
            f'# {_title(edit.content)}\n\n'
            f'Applies when: {applies}\n\n'
            f'source_repo: {member["source_repo"] or "unknown"}\n'
            f'task_id: {member["task_id"]}\n'
            f'experience_id: {member["id"]}\n\n'
            f'{edit.content.strip()}\n')
    files['SKILL.md'], _ = _ensure_link(files.get('SKILL.md', ''), path, applies)
    return path


def _ensure_link(text, path, applies):
    if f']({path})' in text:
        return text, False
    line = f'- [{path}]({path}) — Read when {applies}.'
    span = _section_span(text, '## Reference Files')
    if span is None:
        return text.rstrip('\n') + f'\n\n## Reference Files\n\n{line}\n', True
    return _insert_at(text, _section_insert(text, span), line), True


def _receipt(member, status, repaired, method, reason, *, saved_path=None, final_target=None):
    edit = member['edit']
    target = final_target or {'file': saved_path or edit.file, 'target_section': edit.target_section,
                              'method': method}
    return {'edit_id': member['id'], 'task_id': member['task_id'], 'source_repo': member['source_repo'],
            'index': member['index'], 'operation': edit.op, 'original_target': _public_edit(edit),
            'final_target': target, 'repaired': repaired, 'repair_method': method, 'status': status,
            'reason': reason, 'saved_path': saved_path or edit.file,
            'original_content': edit.content, 'experience_id': member['id']}


def _audit(files, receipt):
    status = receipt['status']
    edit = SimpleNamespace(**{name: receipt['original_target'].get(name, '') for name in _FIELDS})
    text = files.get(receipt['saved_path'], files.get(edit.file, ''))
    ok = True
    if status == 'applied' and receipt['repair_method'] == 'reference_link':
        ok = all(f"]({path})" in files.get('SKILL.md', '') for path in receipt['final_target'].get('paths', []))
    elif status == 'applied':
        ok = _verified(text if edit.op != 'delete_file' else '', edit) if edit.op != 'create' else _contains(
            files.get(edit.file, ''), edit.content)
        if edit.op == 'delete_file':
            ok = edit.file not in files
    elif status == 'already_present':
        ok = edit.op == 'delete_file' and edit.file not in files or _satisfied(files.get(edit.file, ''), edit)
    elif status == 'saved_as_reference':
        ok = _contains(files.get(receipt['saved_path'], ''), receipt['original_content']) and (
            f"]({receipt['saved_path']})" in files.get('SKILL.md', ''))
    elif status == 'pending_integration':
        ok = bool(receipt['original_content'] or receipt['original_target'])
    if ok:
        return receipt
    failed = dict(receipt)
    failed.update(status='pending_integration', reason='audit_mismatch', repaired=receipt['repaired'])
    return failed


def _changelog(receipts):
    lines = []
    for item in receipts:
        label = item['operation']
        if item['status'] == 'applied':
            lines.append(f"Applied {label} to {item['final_target'].get('file', item['saved_path'])}")
        elif item['status'] == 'already_present':
            lines.append(f"Already present: {label} in {item['saved_path']}")
        elif item['status'] == 'saved_as_reference':
            lines.append(f"Saved experience {item['edit_id']} at {item['saved_path']}")
        else:
            lines.append(f"Pending integration: {label} in {item['saved_path']}: {item['reason']}")
    return lines


def _blocks(text, path):
    front, body = _frontmatter(text)
    offset = len(front)
    blocks, fence, start, chunk, heading = [], None, None, [], ''
    lines = body.split('\n')
    cursor = offset

    def close(end):
        if start is None:
            return
        snippet = '\n'.join(chunk)
        if snippet.strip():
            blocks.append({'block_id': f'b{len(blocks):04d}', 'heading': heading, 'text': snippet,
                           'start': start, 'end': end})

    for line in lines:
        line_end = cursor + len(line)
        marker = _FENCE.match(line)
        if fence:
            chunk.append(line)
            if (marker and marker.group(2)[0] == fence[0] and len(marker.group(2)) >= fence[1]
                    and not marker.group(3).strip()):
                fence = None
                close(line_end)
                start, chunk = None, []
        elif marker:
            close(cursor - 1 if cursor else 0)
            fence = (marker.group(2)[0], len(marker.group(2)))
            start, chunk = cursor, [line]
        elif re.match(r'^#{1,6}[ \t]+\S', line):
            close(cursor - 1 if cursor else 0)
            heading = line.strip()
            start, chunk = cursor, [line]
            close(line_end)
            start, chunk = None, []
        elif not line.strip():
            close(cursor - 1 if cursor else 0)
            start, chunk = None, []
        else:
            if start is None:
                start = cursor
            chunk.append(line)
        cursor = line_end + 1
    close(len(text))
    for block in blocks:
        block['end'] = min(len(text), max(block['start'], block['end']))
        while block['end'] > block['start'] and text[block['end'] - 1] == '\n':
            block['end'] -= 1
        block['text'] = text[block['start']:block['end']]
        block['path'] = path
    return [block for block in blocks if block['text'].strip()]


def _section_span(text, header, find_bounds=None):
    if not header or not str(header).strip():
        return None
    if find_bounds is None:
        find_bounds = _upstream()[0]
    lines = text.split('\n')
    bounds = find_bounds(lines, header)
    if bounds is None:
        wanted = _norm(header)
        for index, line in enumerate(lines):
            if _norm(line) == wanted:
                level = len(header.strip()) - len(header.strip().lstrip('#'))
                end = len(lines)
                for follower in range(index + 1, len(lines)):
                    if lines[follower].startswith('#'):
                        follower_level = len(lines[follower]) - len(lines[follower].lstrip('#'))
                        if follower_level <= level:
                            end = follower
                            break
                bounds = (index, end)
                break
    if bounds is None:
        return None
    offsets, cursor = [], 0
    for line in lines:
        offsets.append(cursor)
        cursor += len(line) + 1
    start = offsets[bounds[0]]
    end = offsets[bounds[1]] if bounds[1] < len(offsets) else len(text)
    return start, end


def _section_insert(text, span):
    section = text[span[0]:span[1]]
    trimmed = section.rstrip('\n')
    return span[0] + len(trimmed)


def _insert_at(text, point, content):
    body = content.strip('\n')
    return text[:point].rstrip('\n') + '\n\n' + body + '\n' + text[point:].lstrip('\n')


def _unique_span(text, needle):
    if needle and text.count(needle) == 1:
        start = text.find(needle)
        return start, start + len(needle)
    collapsed, starts, ends = _collapse(text)
    wanted = _norm(needle)
    if not wanted:
        return None
    found = []
    cursor = 0
    while True:
        index = collapsed.find(wanted, cursor)
        if index < 0:
            break
        found.append(index)
        cursor = index + 1
    if len(found) != 1:
        return None
    index = found[0]
    return starts[index], ends[index + len(wanted) - 1]


def _collapse(text):
    chars, starts, ends = [], [], []
    index, size = 0, len(text)
    while index < size:
        if text[index].isspace():
            finish = index + 1
            while finish < size and text[finish].isspace():
                finish += 1
            if chars:
                chars.append(' ')
                starts.append(index)
                ends.append(finish)
            index = finish
            continue
        chars.append(text[index])
        starts.append(index)
        ends.append(index + 1)
        index += 1
    if chars and chars[-1] == ' ':
        chars.pop()
        starts.pop()
        ends.pop()
    return ''.join(chars), starts, ends


def _upstream():
    from rsi.skills.trace2skill.upstream import load
    loaded = load()
    return loaded[6], loaded[7]


def as_edit(edit):
    if isinstance(edit, SimpleNamespace):
        return edit
    return SimpleNamespace(**{name: _value(edit, name) for name in _FIELDS})


def _value(edit, name):
    if isinstance(edit, dict):
        value = edit.get(name, '')
    else:
        value = getattr(edit, name, '')
    return value or ''


def _public_edit(edit):
    return {name: _value(edit, name) for name in _FIELDS}


def _markdown(path):
    return path == 'SKILL.md' or (isinstance(path, str) and path.startswith('references/') and path.endswith('.md'))


def _frontmatter(text):
    if text.startswith('---\n'):
        end = text.find('\n---\n', 4)
        if end > 0:
            return text[:end + 5], text[end + 5:]
    return '', text


def _norm(text):
    return re.sub(r'\s+', ' ', text).strip()


def _contains(text, needle):
    wanted = _norm(needle)
    return bool(wanted) and wanted in _norm(text)


def _title(content):
    match = re.search(r'\*\*(.+?)\*\*', content)
    title = match.group(1).strip().rstrip(':') if match else 'Recovered experience'
    title = re.sub(r'[\[\]<>]', '', title)[:180].strip()
    return title or 'Recovered experience'


def _applies(content):
    raw = re.sub(r'\*+', '', content or '')
    raw = re.sub(r'\s+', ' ', raw).strip()
    sentence = re.split(r'(?<=[.。])\s', raw)[0]
    sentence = re.sub(r'[\[\]<>]', '', sentence)[:280].strip(' -')
    return sentence or 'the recovered experience applies'


def replay_saved_patches(initial, patches, output, source_repo):
    """Replay original final patches from the training seed into a new SkillStore.

    Historical run directories are only read. The new store starts at the seed snapshot
    and advances one task at a time, so an older patch cannot overwrite a later edit.
    """
    from pathlib import Path
    from rsi.core.common import read_json, write_json
    from rsi.skills.directory import INDEX_PATH, read_skill_directory
    from rsi.skills.index import build_index
    from rsi.skills.store import SkillStore

    output = Path(output)
    seed = read_skill_directory(initial, directory_schema=0)
    if INDEX_PATH not in seed:
        # This training seed predates the managed index. Add one so the recovered
        # version is readable by the current retriever; the historical snapshot stays unchanged.
        seed[INDEX_PATH] = build_index(seed)
    store = SkillStore(output / 'skills', seed, directory_schema=1)
    report = {'seed_version': store.current().name, 'source_repo': source_repo, 'tasks': []}
    for task_id, patch_path in patches:
        current = read_skill_directory(store.current(), directory_schema=1)
        patch = read_json(patch_path)
        result = write_patch(current, patch['edits'], task_id=task_id, source_repo=source_repo,
                             repair=None, model_changelog=patch.get('changelog_entries') or [])
        changes = {path: result['files'].get(path) for path in set(current) | set(result['files'])
                   if editable(path) and result['files'].get(path) != current.get(path)}
        version = store.current().name
        if changes:
            candidate, _diff = store.propose(changes)
            store.record(candidate, True, {'task_id': task_id, 'decision': result['completion'],
                                            'method': 'trace2skill-replay'})
            version = candidate.name
            reread = read_skill_directory(candidate, directory_schema=1)
            for receipt in result['receipts']:
                checked = _audit(reread, receipt)
                receipt['reread_status'] = checked['status']
                if checked['status'] != receipt['status']:
                    receipt['status'] = checked['status']
                    receipt['reason'] = checked['reason'] or 'reread_mismatch'
            second = write_patch(reread, patch['edits'], task_id=task_id, source_repo=source_repo, repair=None)
            receipt_ids = [item['edit_id'] for item in result['receipts']]
            result['second_pass'] = [{'edit_id': item['edit_id'], 'status': item['status']}
                                     for item in second['receipts']]
            result['second_pass_same_ids'] = [item['edit_id'] for item in second['receipts']] == receipt_ids
            result['second_pass_changed'] = any(
                second['files'].get(path) != reread.get(path) for path in set(reread) | set(second['files']))
        report['tasks'].append({'task_id': task_id, 'version': version, 'completion': result['completion'],
                                 'structure_repaired': result['structure_repaired'],
                                 'model_changelog': result['model_changelog'],
                                 'changelog': result['changelog'], 'receipts': result['receipts'],
                                 'second_pass': result.get('second_pass', []),
                                 'second_pass_same_ids': result.get('second_pass_same_ids'),
                                 'second_pass_changed': result.get('second_pass_changed')})
        write_json(output / 'replay-report.json', report)
    report['version'] = store.current().name
    write_json(output / 'replay-report.json', report)
    return report
