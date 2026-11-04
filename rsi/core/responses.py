"""Archive one JSON generation; transport backoff belongs to Model."""
from pathlib import Path
from rsi.core.common import parse_object, write_json
from rsi.core.model import object_messages


def generate_object(model, instruction, data, folder, name):
    folder = Path(folder)
    messages = object_messages(instruction, data)
    write_json(folder / f'{name}.request.json', {'messages': messages})
    previous_log = getattr(model, 'stream_log', None)
    model.stream_log = folder / f'{name}.stream.jsonl'
    try:
        response = model.query(messages)
        write_json(folder / f'{name}.response.json', response)
        return parse_object(response['content'])
    except Exception as exc:
        write_json(folder / f'{name}.error.json', {'error': f'{type(exc).__name__}: {exc}'})
        raise
    finally:
        model.stream_log = previous_log
        write_json(folder / f'{name}-usage.json', model.stats())


def full_evidence(results, purpose='selector'):
    from rsi.agent.evidence import build_evidence
    limit = max(1, max(sum(m.get('role') == 'assistant' for m in r['messages']) for r in results))
    evidence = build_evidence(results, limit, purpose)
    if any(r['steps_omitted'] for r in evidence['trajectories']):
        raise RuntimeError('Core evidence must contain every solver step')
    return evidence, limit
