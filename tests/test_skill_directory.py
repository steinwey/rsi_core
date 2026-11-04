import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from rsi.agent.runner import run_experiment
from rsi.core.common import read_json
from rsi.skills.store import SkillStore, load_initial_skill, SkillProposalRejected
from test_core_baseline import Backend, Models, test_config


class DirectoryTests(unittest.TestCase):
    def test_reference_versioning_deletion_and_boundaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            seed, _ = load_initial_skill()
            store = SkillStore(Path(tmp), seed)
            guide = seed['SKILL.md'] + '\nSee [contracts](references/contracts.md).\n'
            candidate, diff = store.propose({'SKILL.md': guide, 'references/contracts.md': '# Contracts\n' + 'Details. ' * 2000})
            store.record(candidate, True, {})
            loaded, _ = load_initial_skill(candidate)
            self.assertIn('references/contracts.md', loaded)
            self.assertIn('references/contracts.md', diff)
            with self.assertRaisesRegex(SkillProposalRejected, 'link target'):
                store.propose({'references/contracts.md': None})
            removed, _ = store.propose({'SKILL.md': seed['SKILL.md'], 'references/contracts.md': None})
            self.assertFalse((removed / 'references/contracts.md').exists())
            for name in ('../escape.md', 'references/../escape.md', 'references/C:bad.md', 'scripts/new.py'):
                with self.assertRaises(SkillProposalRejected):
                    store.propose({name: 'bad'})

    def test_actual_map_reduce_apply_creates_reference_and_next_task_inherits(self):
        class ReferenceModels(Models):
            def __call__(owner, config):
                client = super().__call__(config)
                query = client.query
                def respond(messages, **kwargs):
                    answer = query(messages, **kwargs)
                    if client.name == 'judge' and answer['content'].startswith('```json'):
                        data = json.loads(answer['content'][7:-3])
                        data['edits'] = [
                            {'file': 'SKILL.md', 'op': 'append_to_section', 'target_section': '# Test and diagnose',
                             'content': 'For state contracts, read [details](references/contracts.md).'},
                            {'file': 'references/contracts.md', 'op': 'create', 'content': '# Contracts\nPreserve raw state and verify resolved output separately.'}]
                        answer['content'] = '```json\n' + json.dumps(data) + '\n```'
                    return answer
                client.query = respond
                return client
        Backend.barrier, Backend.instances = threading.Barrier(5), []
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'run'
            tasks = [{'id': f'task-{i}', 'benchmark': 'swe', 'prompt': 'Fix state'} for i in range(2)]
            with patch('rsi.agent.runner.functional_gate', return_value={'passed': True}):
                reports = run_experiment(tasks, test_config(), output, backend_factory=Backend, model_factory=ReferenceModels())
            first = read_json(output / 'task-0/update.json')
            self.assertTrue(first['accepted'])
            self.assertEqual(reports[1]['skill_version'], first['after_skill_version'])
            analysis = read_json(output / 'task-1/trace2skill/rollout-0/input.json')
            original = read_json(output / 'task-1/rollout-0/trajectory.json')
            self.assertEqual(analysis['trajectory']['messages'], original['messages'])
            self.assertIn('references/contracts.md', analysis['current_skill'])
            self.assertFalse(list(output.rglob('compression')))
            self.assertNotIn('accuracy', read_json(output / 'metrics.json'))
