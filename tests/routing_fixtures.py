"""P2 offline fixtures: synthetic counts NEVER claim target-model token accuracy."""
from rsi.skills.budget import CounterInfo
from rsi.skills.directory import INDEX_PATH
from rsi.skills.index import build_index, validate_index
import yaml


class CharacterCounter:
    def __init__(self, model='mock-solver'):
        self.info = CounterInfo(model, 'test-unicode-codepoint-v1', 'test_only',
                                'Synthetic fixture: one Unicode codepoint per test token; not a model tokenizer.')
        self.calls = []

    def count(self, text):
        self.calls.append(text)
        return len(text)


def files(refs=None, root='# Core\nVerify changes.\n', metadata=None):
    value = {'SKILL.md': root, 'scripts/check.py': 'print(1)\n'} | (refs or {})
    value[INDEX_PATH] = build_index(value)
    index = validate_index(value)
    for entry in index['entries']:
        entry.update(title='Reference', description='Technical notes', keywords=['scope'], applies_when=['Relevant task'])
        entry.update((metadata or {}).get(entry['path'], {}))
    value[INDEX_PATH] = yaml.safe_dump(index, sort_keys=True, allow_unicode=True)
    validate_index(value)
    return value
