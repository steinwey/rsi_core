"""Best-effort command/output evidence, never complete syscall tracing."""
import hashlib
import json
import posixpath
import re
import shlex
from dataclasses import asdict
from pathlib import Path
from rsi.core.common import read_json, write_json
from rsi.skills.budget import token_counter, check_counter, count_text
from rsi.skills.index import sha
from rsi.skills.routing import settings as routing_settings
from rsi.skills.store import SKILL_ROOT

READERS = ('cat', 'sed', 'head', 'tail', 'grep', 'rg', 'awk', 'python', 'python3')


def detect(command, cwd):
    try:
        tokens = shlex.split(command)
    except ValueError:
        return [], False, False
    base = cwd or '/testbed'
    change = re.search(r'(?:^|[;&]\s*)cd\s+(/opt/rsi-skills(?:/[A-Za-z0-9_./-]*)?)', command)
    if change:
        base = posixpath.normpath(change[1])
    candidates = re.findall(r'/opt/rsi-skills/[A-Za-z0-9_./-]+', command)
    candidates += re.findall(r"['\"]((?:references|routing)/[A-Za-z0-9_./-]+|SKILL[.]md)['\"]", command)
    candidates += [v.strip(';,') for v in tokens if not v.startswith('-') and ('/' in v or v.endswith(('.md', '.yaml', '.py')))]
    paths = []
    for value in candidates:
        full = posixpath.normpath(posixpath.join(base, value))
        if full.startswith(SKILL_ROOT + '/'):
            rel = full[len(SKILL_ROOT)+1:]
            if rel not in paths:
                paths.append(rel)
    reader = any(re.search(r'(?:^|[;&|\s])(?:/usr/bin/|/bin/)?' + name + r'(?:\s|$)', command) for name in READERS)
    reader = reader or bool(re.search(r'<\s*/opt/rsi-skills/', command))
    simple = (len(tokens) == 2 and tokens[0] in ('cat', '/bin/cat', '/usr/bin/cat')
              and not any(c in command for c in '$`;&|<>\n'))
    return paths, simple, reader


def sha_bytes(value):
    return hashlib.sha256(value).hexdigest()


def audit(folder, files, context, config, result, *, counter_factory=None, default_cwd="/testbed"):
    from rsi.skills.access import settings
    options = settings(config)
    folder = Path(folder)
    route = read_json(folder.parent / 'skill-route.json')
    summary = {'audit_scope': 'best_effort_command_and_observation; not OS file tracing',
        'initial_skill_tokens': context.initial_skill_tokens,
        'initial_skill_tokens_estimated': context.initial_skill_tokens if route['counter']['accuracy'] == 'estimated' else None,
        'initial_counter': route['counter'], 'automatically_injected_references': list(context.selected_references),
        'autonomous_observation_tokens_estimated': None, 'autonomous_read_events': None,
        'unique_references_read': None, 'solver_input_tokens_api': result['usage']['input_tokens'],
        'solver_output_tokens_api': result['usage']['output_tokens'], 'router_model_calls': 0,
        'solver_usage_source': 'reported model usage; not local observation estimates',
        'cost_addition_allowed': False, 'status': 'disabled' if not options['audit_reads'] else 'ok'}
    if result['usage'].get('calls', 0) and not result['usage']['input_tokens'] and not result['usage']['output_tokens']:
        summary['solver_input_tokens_api'] = summary['solver_output_tokens_api'] = None
        summary['solver_usage_source'] = 'unknown: no token usage reported for model calls'
    if not options['audit_reads']:
        write_json(folder / 'skill-access-summary.json', summary)
        return summary
    counter = None
    if options['record_observation_cost']:
        try:
            routing = routing_settings(config)
            counter = counter_factory(config['model']) if counter_factory else token_counter(
                config['model'], mode=routing['budget_mode'], estimated=routing.get('estimated_counter'))
            check_counter(counter, config['model']['name'], mode=routing['budget_mode'])
        except Exception as exc:
            counter = None
            summary['counter_error'] = type(exc).__name__
    events = [{'event_type': 'automatic_injection', 'skill_version': context.skill_version, 'route_id': context.route_id,
               'paths': ['SKILL.md', *context.selected_references], 'tokens': context.initial_skill_tokens,
               'counter': route['counter']}]
    confirmed, possible = set(), set()
    reads = unknown = observations = total = 0
    count_known = counter is not None
    for path in sorted((folder / 'commands').glob('[0-9]*.json')):
        record = read_json(path)
        paths, simple, reader = detect(record['command'], record.get('cwd') or default_cwd)
        potential = bool(paths) and reader
        output = record.get('output')
        raw = b''
        if output:
            name = output['path']
            if Path(name).name != name:
                raise ValueError('Invalid command output evidence path')
            raw = (path.parent / name).read_bytes()
            if sha_bytes(raw) != output['sha256']:
                raise ValueError('Command output evidence hash mismatch')
        success = record.get('returncode') == 0 and not record.get('error')
        corroborated = (potential and simple and len(paths) == 1 and success and paths[0] in files
                        and bool(raw) and raw == files[paths[0]].encode('utf-8'))
        classification = 'content_confirmed' if corroborated else 'inferred' if potential and success else 'failed_or_unknown'
        if classification == 'failed_or_unknown':
            unknown += 1
        if potential and success:
            reads += 1
        if corroborated:
            confirmed.update(p for p in paths if p.startswith('references/'))
        if potential:
            possible.update(p for p in paths if p.startswith('references/'))
        number = record['assistant_message'] + 1
        messages = result['messages']
        observation = messages[number]['content'] if number < len(messages) and messages[number]['role'] == 'user' else None
        estimate = None
        if potential and observation is not None:
            observations += 1
            if counter is not None:
                try:
                    estimate = count_text(counter, observation)
                    total += estimate
                except Exception:
                    count_known = False
        elif potential:
            count_known = False
        events.append({'event_type': 'autonomous_skill_read' if potential else 'unclassified_command',
            'rollout': result['rollout'], 'skill_version': context.skill_version, 'route_id': context.route_id,
            'paths': paths, 'classification': classification, 'read_detected': True if corroborated else None,
            'evidence_basis': 'exact full-file output and simple cat; not syscall proof' if corroborated else 'command inference only',
            'command_index': record['sequence'], 'command': record['command'], 'returncode': record.get('returncode'),
            'error': record.get('error'), 'command_record': 'commands/' + path.name,
            'command_record_sha256': sha_bytes(path.read_bytes()), 'raw_output': output,
            'observation_message': number if observation is not None else None,
            'observation_sha256': sha(observation) if observation is not None else None,
            'observation_chars': len(observation) if observation is not None else None,
            'observation_tokens_estimated': estimate, 'token_counter': asdict(counter.info) if counter else None,
            'cost_scope': 'whole observation once per command; mixed output not attributable per file'})
    summary.update(autonomous_read_events=reads or None,
        detected_read_command_count=reads, unclassified_or_failed_commands=unknown,
        unique_references_read=sorted(confirmed) if confirmed else None, possible_references_read=sorted(possible),
        observation_count=observations,
        autonomous_observation_tokens_estimated=total if count_known and reads else None,
        observation_counter=asdict(counter.info) if counter else None, absence_of_events_proves_no_reads=False,
        observation_cost_scope='detected candidate commands only; not all Skill access or full API cost')
    (folder / 'skill-access.jsonl').write_text(''.join(json.dumps(e, ensure_ascii=False) + '\n' for e in events), encoding='utf-8', newline='\n')
    write_json(folder / 'skill-access-summary.json', summary)
    return summary
