"""Bridge official Trace2Skill clients to RSI transport, deadlines, usage and archives."""
import threading
from pathlib import Path
from rsi.core.common import write_json
from rsi.core.model import model_usage


class RecordedClient:
    def __init__(self, config, folder, factory, deadline, *, abort_event=None):
        self.config, self.folder, self.factory, self.deadline = config, Path(folder), factory, deadline
        self.models = []
        self.lock = threading.Lock()
        self.abort_event = abort_event

    def chat(self, messages, settings=None):
        if self.abort_event is not None and self.abort_event.is_set():
            raise RuntimeError('Trace2Skill stopped after a learning-stage error')
        with self.lock:
            model = self.factory(dict(self.config))
            model.deadline = self.deadline
            self.models.append(model)
            number = len(self.models)
        stem = self.folder / f'call-{number:04d}'
        model.stream_log = stem.with_suffix('.stream.jsonl')
        payload = [{'role': m.role, 'content': m.content} for m in messages]
        write_json(stem.with_suffix('.request.json'), {'model': self.config['name'], 'messages': payload})
        try:
            stop = settings.stop if settings is not None else None
            local_stop = stop and self.config['name'] == 'gpt-6-luna'
            options = {'stop': stop} if stop and not local_stop else {}
            reply = model.query(payload, **options)
            write_json(stem.with_suffix('.response.json'), reply)
            if getattr(model, 'last_finish_reason', None) == 'length':
                raise ValueError('Trace2Skill response hit the model output limit')
            if not isinstance(reply.get('content'), str) or not reply['content'].strip():
                raise ValueError('Trace2Skill received an empty response')
            content = reply['content']
            if local_stop:
                # Luna's API rejects stop sequences. Keep the raw reply and billed usage.
                end = min((content.find(marker) for marker in stop if marker in content), default=len(content))
                content = content[:end]
            return content
        except Exception as exc:
            if self.abort_event is not None:
                self.abort_event.set()
            write_json(stem.with_suffix('.error.json'), {'error': f'{type(exc).__name__}: {exc}'})
            raise
        finally:
            write_json(stem.with_suffix('.usage.json'), model.stats())

    async def chat_async(self, messages, settings=None):
        return self.chat(messages, settings)

    def stats(self):
        return model_usage(*self.models)
