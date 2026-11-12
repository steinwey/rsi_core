"""Runtime idle-loop session. Off mode never constructs one."""
import hashlib
from pathlib import Path

from rsi.core.common import write_json
from rsi.skills.trace2skill.pathology import (
    HARD_STOP_FALLBACK, PathologyTracker, hard_stop_for, normalize_command, pathology_settings, repeat_continues)


REPEAT_NOTE = ('[monitor] This command has returned identical output {count} times with no edits in between. '
               'Consider a different action.')
SKIP_NOTE = ('[monitor] Skipped execution: this command has returned identical output {count} times '
             'with no intervening edits. Last output summary: {summary}. '
             'Use a different command or proceed with the information above.')
STALL_NOTE = ('[monitor] More than half the step budget has been used without an edit. '
              'Consider starting the minimal implementation.')


def open_pathology(config, folder):
    settings = config.get('pathology') or {}
    if not isinstance(settings, dict) or settings.get('mode', 'off') == 'off':
        return None
    return PathologySession(config, folder)


class PathologySession:
    def __init__(self, config, folder):
        settings = pathology_settings(config.get('pathology'))
        if settings['mode'] == 'off':
            raise ValueError('off mode does not open a pathology session')
        self.mode = settings['mode']
        self.k1 = settings['k1']
        self.configured_hard_stop = settings['hard_stop']
        self.resolved_hard_stop = hard_stop_for(config['model']['name'], settings['hard_stop'])
        self.hard_stop = self.resolved_hard_stop if self.mode == 'enforce' else None
        if self.hard_stop is HARD_STOP_FALLBACK:
            self.hard_stop = None
        self.tracker = PathologyTracker(k1=self.k1, k2=3, k3=3, step_limit=config.get('step_limit'))
        self.folder = Path(folder)
        self.events = []
        self.last_output = {}
        self.stall_advised = False

    def will_skip(self, command):
        if self.mode != 'enforce' or not self.hard_stop:
            return False
        chain = self.tracker.chains.get(normalize_command(command)) or []
        if not chain:
            return False
        step = {'step': len(self.tracker.steps) + 1, 'sha': chain[0]['sha']}
        return repeat_continues(chain, step, self.tracker.steps) and len(chain) + 1 >= self.hard_stop

    def commit_skip(self, command):
        normalized = normalize_command(command)
        chain = self.tracker.chains.get(normalized) or []
        summary = (self.last_output.get(normalized) or '').replace('\n', ' ')[:200]
        note = SKIP_NOTE.format(count=len(chain), summary=summary)
        events = self.tracker.observe(command, chain[0]['sha'] if chain else None, False, False)
        extra = self._record(events, skipped=True)
        return note if not extra else note + '\n' + extra

    def after_output(self, command, output):
        text = output.get('output') or ''
        if isinstance(text, bytes):
            text = text.decode('utf-8', errors='replace')
        self.last_output[normalize_command(command)] = text
        digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
        note = self._record(self.tracker.observe(command, digest, False, False), skipped=False)
        return note or None

    def observe_unexecuted(self, *, truncated, parse_failed):
        self._record(self.tracker.observe(None, None, truncated, parse_failed), skipped=False)
        return None

    def _record(self, events, *, skipped):
        lines = []
        for event in events:
            action = 'none'
            if event['type'] == 'repeated_command' and skipped:
                action = 'skip'
            elif event['type'] == 'repeated_command' and self.mode in ('advise', 'enforce'):
                action = 'advise'
                lines.append(REPEAT_NOTE.format(count=event['count']))
            elif event['type'] == 'zero_edit_stall' and self.mode in ('advise', 'enforce') and not self.stall_advised:
                action = 'advise'
                self.stall_advised = True
                lines.append(STALL_NOTE)
            self.events.append({'type': event['type'], 'step': event['step'], 'count': event['count'], 'action': action})
        self.write()
        return '\n'.join(lines)

    def write(self):
        write_json(self.folder / 'pathology-live.json', {
            'mode': self.mode,
            'thresholds': {'k1': self.k1, 'k2': 3, 'k3': 3, 'hard_stop': self.resolved_hard_stop},
            'events': self.events})
