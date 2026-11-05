"""Deterministic pre-solve BM25 over a verified managed snapshot; no model calls."""
import json
import math
import re
import time
from collections import Counter
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Protocol

from rsi.core.common import digest, write_json
from rsi.skills.directory import INDEX_PATH, read_skill_directory
from rsi.skills.index import sha, validate_index
from rsi.skills.budget import BudgetError, SkillContext, check_counter, select_context, token_counter

ALGORITHM = 'bm25-skill-v1'
TOKENIZATION = 'identifier-cjk-v1'
FIELD_WEIGHTS = {'title': 1, 'description': 1, 'keywords': 1, 'applies_when': 1, 'body': 1}
STOP_WORDS = frozenset('a an and are as at be by for from in is it of on or that the this to was were with'.split())
DEFAULTS = {'enabled': False, 'retrieval_method': 'bm25', 'top_k': 2,
            'max_initial_skill_tokens': 4000, 'allow_zero_matches': True,
            'budget_mode': 'strict', 'bm25': {'k1': 1.2, 'b': 0.75}}


def settings(config):
    block = config.get('skill_routing', {})
    if not isinstance(block, dict) or block.keys() - (DEFAULTS.keys() | {'estimated_counter'}):
        raise ValueError('Invalid skill_routing configuration')
    value = DEFAULTS | block
    for key in ('enabled', 'allow_zero_matches'):
        if type(value[key]) is not bool:
            raise ValueError(f'skill_routing.{key} must be boolean')
    for key, maximum in (('top_k', 1000), ('max_initial_skill_tokens', 1000000)):
        if type(value[key]) is not int or not 1 <= value[key] <= maximum:
            raise ValueError(f'skill_routing.{key} must be an integer in [1, {maximum}]')
    if value['retrieval_method'] != 'bm25' or value['budget_mode'] not in ('strict', 'estimated'):
        raise ValueError('Only BM25 retrieval and strict/estimated budget modes are supported')
    if value['budget_mode'] == 'estimated':
        from rsi.skills.tokenizers import validate_estimated
        validate_estimated(config.get('model', {}), value.get('estimated_counter'))
    elif 'estimated_counter' in block:
        raise ValueError('estimated_counter is forbidden in strict mode')
    bm = value['bm25']
    if not isinstance(bm, dict) or bm.keys() - {'k1', 'b'}:
        raise ValueError('Invalid skill_routing.bm25 configuration')
    bm = DEFAULTS['bm25'] | bm
    if any(type(v) not in (float, int) or not math.isfinite(v) for v in bm.values()):
        raise ValueError('BM25 parameters must be finite numbers')
    if not 0 < bm['k1'] <= 100 or not 0 <= bm['b'] <= 1:
        raise ValueError('BM25 requires 0 < k1 <= 100 and 0 <= b <= 1')
    return value | {'bm25': bm}


def tokenize(text):
    result = []
    for match in re.finditer(r'[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*|[\u3400-\u9fff]+', text):
        word = match.group()
        if '\u3400' <= word[0] <= '\u9fff':
            result.extend(word[i:i+2] for i in range(max(1, len(word)-1)))
            continue
        parts = [word.lower()]
        for component in re.split(r'[_.]+', word):
            parts.append(component.lower())
            parts.extend(s.lower() for s in re.findall(r'[A-Z]+(?=[A-Z][a-z]|[0-9]|$)|[A-Z]?[a-z]+|[0-9]+', component))
        # One occurrence of each decomposition per source lexeme, preserving repeated words.
        result.extend(p for p in dict.fromkeys(parts) if p and p not in STOP_WORDS)
    return tuple(result)


def _phrase_tokens(text):
    return re.findall(r'[a-z0-9_]+(?:\.[a-z0-9_]+)*|[\u3400-\u9fff]', text.lower())


def exclusion_match(query, conditions):
    query = _phrase_tokens(query)
    for condition in conditions:
        phrase = _phrase_tokens(condition)
        if phrase and any(query[i:i+len(phrase)] == phrase for i in range(len(query)-len(phrase)+1)):
            return condition
    return None


@dataclass(frozen=True)
class Candidate:
    path: str
    score: float
    exclusion: str | None = None


class Reranker(Protocol):
    def rerank(self, query: str, candidates: tuple[Candidate, ...]) -> tuple[Candidate, ...]: ...


def retrieve_bm25(query, files, index, *, k1=1.2, b=0.75):
    """Standard positive-IDF BM25, equal-weight concatenated fields, unique query terms.

    Caller must validate the snapshot/index. exclude_when is phrase filtering only;
    applies_when contributes lexical evidence, not an inferred semantic constraint.
    """
    entries = sorted(index['entries'], key=lambda e: e['path'])
    documents = []
    for entry in entries:
        fields = [entry['title'], entry['description'], *entry['keywords'],
                  *entry['applies_when'], files[entry['path']]]
        documents.append(Counter(tokenize('\n'.join(fields))))
    n = len(documents)
    if not n:
        return ()
    lengths = [sum(doc.values()) for doc in documents]
    average = sum(lengths) / n or 1.0
    query_terms = sorted(set(tokenize(query)))
    frequencies = {term: sum(term in doc for doc in documents) for term in query_terms}
    candidates = []
    for entry, doc, length in zip(entries, documents, lengths):
        score = 0.0
        for term in query_terms:
            tf = doc[term]
            if tf:
                df = frequencies[term]
                idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
                score += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * length / average))
        candidates.append(Candidate(entry['path'], score, exclusion_match(query, entry['exclude_when'])))
    return tuple(sorted(candidates, key=lambda c: (-c.score, c.path)))


def stable_json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


@dataclass(frozen=True)
class RouteResult:
    context: SkillContext
    record_json: str

    def record(self):
        return json.loads(self.record_json)


def route(task, skill, config, *, folder=None, counter_factory=None):
    """Exactly one raw prompt + one verified immutable snapshot -> one frozen context."""
    started = time.monotonic()
    options = settings(config)
    record = {'schema': 1, 'status': 'failed', 'task_id': task['id'], 'task_hash': sha(task['prompt']),
              'skill_version': Path(skill).name, 'index_hash': None, 'algorithm': ALGORITHM,
              'tokenization': TOKENIZATION, 'field_weights': FIELD_WEIGHTS,
              'retrieval_method': options['retrieval_method'], 'top_k': options['top_k'],
              'parameters': options, 'candidate_scores': [], 'selected_references': [],
              'excluded_references': [], 'route_id': None, 'context_hash': None,
              'initial_skill_tokens': None, 'budget_limit': options['max_initial_skill_tokens'],
              'counter': {'model': config['model']['name'], 'accuracy': 'unsupported', 'identity': None},
              'context_file': 'skill-context.txt'}
    try:
        from rsi.skills.directory import directory_schema
        if not options['enabled'] or directory_schema(config) != 1:
            raise ValueError('Routing requires enabled routing and P0 directory schema 1')
        files = read_skill_directory(skill, directory_schema=1)
        record['index_hash'] = sha(files[INDEX_PATH]) if INDEX_PATH in files else None
        try:
            index = validate_index(files)
        except ValueError as exc:
            raise BudgetError('index_invalid', str(exc)) from exc
        if digest(files) != Path(skill).name:
            raise BudgetError('skill_version_mismatch', 'Skill snapshot content differs from its version.')
        candidates = retrieve_bm25(task['prompt'], files, index, **options['bm25'])
        record['candidate_scores'] = [asdict(c) for c in candidates]
        eligible = []
        for candidate in candidates:
            reason = 'exclude_when' if candidate.exclusion else 'no_positive_match' if candidate.score <= 0 else None
            if reason:
                record['excluded_references'].append({'path': candidate.path, 'reason': reason,
                                                       'condition': candidate.exclusion})
            else:
                eligible.append(candidate.path)
        counter = counter_factory(config['model']) if counter_factory else token_counter(
            config['model'], mode=options['budget_mode'], estimated=options.get('estimated_counter'))
        record['counter'] = asdict(check_counter(counter, config['model']['name'], mode=options['budget_mode']))
        selection_limit = record['budget_limit']
        if options['budget_mode'] == 'estimated':
            from rsi.skills.tokenizers import effective_limit
            selection_limit = effective_limit(selection_limit, options['estimated_counter']['safety_margin'])
            record['estimated_budget'] = {
                'effective_limit': selection_limit, 'safety_margin': options['estimated_counter']['safety_margin'],
                'strict_guarantee': False, 'unit': 'proxy BPE tokens; not provider usage'}
        from rsi.skills.access import settings as access_settings
        access = access_settings(config)
        if access['enabled']:
            record['skill_access'] = access
        selection = select_context(files, eligible, counter, budget_limit=selection_limit, top_k=options['top_k'], access=access)
        record['core_tokens'] = selection.core_tokens
        if not eligible and not options['allow_zero_matches']:
            raise BudgetError('zero_matches_disallowed', 'No applicable positive BM25 match.')
        record['excluded_references'].extend({'path': p, 'reason': why, 'trial_tokens': tokens}
                                              for p, why, tokens in selection.excluded)
        context = selection.context
        record.update(status='ok', selected_references=list(context.selected_references),
                      loaded_files=[{'path': p, 'content_hash': sha(files[p]), 'full_or_partial': 'full', 'unit_ids': []}
                                    for p in context.selected_references],
                      initial_skill_tokens=context.initial_skill_tokens, context_hash=context.context_hash,
                      outcome='references_loaded' if context.selected_references else
                              'no_relevant_references' if not eligible else 'references_over_budget')
        # Timing is observational only; no random IDs, absolute paths or hidden task fields.
        record['route_id'] = sha(stable_json(record))
        context = replace(context, skill_version=record['skill_version'], index_hash=record['index_hash'],
                          route_id=record['route_id'])
        if folder is not None:
            (Path(folder) / 'skill-context.txt').write_bytes(context.text.encode('utf-8'))
    except Exception as exc:
        record.update(reason=getattr(exc, 'code', 'routing_error'), error=str(exc))
        raise
    finally:
        record['seconds'] = time.monotonic() - started
        if folder is not None:
            write_json(Path(folder) / 'skill-route.json', record)
    return RouteResult(context, stable_json(record))


def verify_receipt(output, report, config, task=None):
    """Checkpoint-linked immutable routing artifacts; completed tasks need no tokenizer."""
    if not settings(config)['enabled']:
        return
    from rsi.core.common import read_json, safe_id
    from rsi.skills.directory import reject_links
    import hashlib
    folder = Path(output) / safe_id(report['task_id'])
    receipt_path, context_path = folder / 'skill-route.json', folder / 'skill-context.txt'
    reject_links(receipt_path)
    reject_links(context_path)
    receipt = report.get('skill_routing')
    if not isinstance(receipt, dict):
        raise ValueError('Missing routing receipt')
    if hashlib.sha256(receipt_path.read_bytes()).hexdigest() != receipt.get('route_file_sha256'):
        raise ValueError('Routing receipt hash mismatch')
    record = read_json(receipt_path)
    stable = {key: value for key, value in record.items() if key != 'seconds'}
    stable['route_id'] = None
    files = read_skill_directory(Path(output) / 'skills/versions' / report['skill_version'], directory_schema=1)
    validate_index(files)
    from rsi.skills.access import settings as access_settings
    access = access_settings(config)
    if (record.get('skill_access') != (access if access['enabled'] else None)
            or record.get('status') != 'ok' or record.get('route_id') != sha(stable_json(stable))
            or record.get('parameters') != settings(config)
            or record.get('task_id') != report['task_id']
            or record.get('skill_version') != report['skill_version']
            or record.get('index_hash') != sha(files[INDEX_PATH])
            or digest(files) != report['skill_version']
            or record.get('context_hash') != hashlib.sha256(context_path.read_bytes()).hexdigest()
            or any(record.get(k) != receipt.get(k) for k in ('route_id', 'context_hash', 'index_hash'))
            or (task is not None and record.get('task_hash') != sha(task['prompt']))):
        raise ValueError('Routing artifact identity mismatch')
