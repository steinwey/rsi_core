import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from rsi import ROOT
from rsi.core.common import read_json
from rsi.core.config import load_config
from rsi.agent.runner import run_experiment


def test_config():
    config = load_config(ROOT / 'configs/agent/qwen.yaml')
    for role, name in (('model', 'solver'), ('judge_model', 'judge'), ('skill_editor_model', 'judge'),
                       ('skill_error_model', 'error_analyst'), ('skill_success_model', 'success_analyst')):
        config[role]['name'] = name
    return config


class Environment:
    def execute(self, command, cwd=''):
        return {'returncode': 0, 'output': 'COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\n' if command.startswith('echo ') else ''}

    def get_template_vars(self):
        return {}


class Backend:
    barrier = None
    instances = []

    def __init__(self, task, folder, config):
        self.env, self.folder, self.closed = Environment(), folder, False
        self.instances.append(self)
        self.barrier.wait(timeout=10)

    def artifact(self):
        return 'patch-' + self.folder.name

    def score(self, artifact):
        assert all(b.closed for b in self.instances if b.folder.parent == self.folder.parent)
        return {'success': True, 'hidden': 'HIDDEN_SCORE_MUST_NOT_REACH_ANALYSIS'}

    def close(self):
        self.closed = True


class Models:
    def __init__(self, *, no_changes=False):
        self.requests = []
        self.no_changes = no_changes

    def __call__(self, config):
        owner = self
        class Client:
            def __init__(self):
                self.name, self.n_calls, self.cost, self.deadline = config['name'], 0, 0, None

            def get_template_vars(self):
                return {}

            def query(self, messages, **kwargs):
                self.n_calls += 1
                owner.requests.append((self.name, copy.deepcopy(messages)))
                if self.name == 'solver':
                    return {'content': '```bash\necho COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\n```'}
                prompt = messages[0]['content']
                assert 'HIDDEN_SCORE_MUST_NOT_REACH_ANALYSIS' not in json.dumps(messages)
                if self.name in ('error_analyst', 'success_analyst'):
                    kind = 'Failure' if self.name == 'error_analyst' else 'Success'
                    value = (f'# {kind} Memory Item 1\n\n## Title\nCaller contracts\n\n'
                             '## Description\nPreserve original state.\n\n## Content\n'
                             'Inspect direct readers and verify raw state independently from resolved output.\n')
                    return {'content': value + ('\nACTION: TASK_COMPLETE' if self.name == 'error_analyst' else '')}
                if prompt.startswith('Select'):
                    data = json.loads(messages[1]['content'])
                    assert len(data['trajectories']) == 5
                    return {'content': json.dumps({'rollout': 4, 'reason': 'visible evidence'})}
                value = {'reasoning': 'Retain a supported caller-contract verification procedure.',
                         'edits': [] if owner.no_changes else [{'file': 'SKILL.md', 'op': 'append_to_section',
                            'target_section': '# Test and diagnose', 'content': 'Verify caller assumptions.'}],
                         'changelog_entries': ['Add caller-contract verification']}
                return {'content': '```json\n' + json.dumps(value) + '\n```'}

            def stats(self):
                return {'calls': self.n_calls, 'input_tokens': self.n_calls * 10,
                        'output_tokens': self.n_calls * 5, 'cost_usd': 0}
        return Client()


class BaselineTests(unittest.TestCase):
    def setUp(self):
        Backend.barrier, Backend.instances = threading.Barrier(5), []

    def test_full_trace2skill_path_and_next_task_inheritance(self):
        tasks = [{'id': f'example-{i}', 'benchmark': 'swe', 'prompt': 'Fix the visible issue'} for i in range(2)]
        models = Models()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'run'
            with patch('rsi.agent.runner.functional_gate', return_value={'passed': True}):
                reports = run_experiment(tasks, test_config(), output, backend_factory=Backend, model_factory=models)
            self.assertEqual(len(reports), 2)
            first = read_json(output / 'example-0/update.json')
            second = read_json(output / 'example-1/update.json')
            self.assertEqual(first['after_skill_version'], second['before_skill_version'])
            self.assertTrue(first['accepted'])
            for i in range(5):
                route = read_json(output / f'example-1/rollout-{i}/trajectory.json')
                self.assertEqual(route['skill_version'], first['after_skill_version'])
                self.assertTrue(route['skill_injected'])
                self.assertNotIn('reflection', route)
            self.assertTrue(all(b.closed for b in Backend.instances))
            self.assertEqual(read_json(output / 'run-info.json')['status'], 'completed')
            self.assertTrue((output / 'example-0/trace2skill/patches').exists())
            self.assertFalse((output / 'example-0/updater.request.json').exists())
            self.assertEqual(read_json(output / 'metrics.json')['input_tokens'], 10 * len(models.requests))

    def test_no_change_does_not_grow_skill(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'run'
            with patch('rsi.agent.runner.functional_gate', return_value={'passed': True}):
                run_experiment([{'id': 'example', 'benchmark': 'swe', 'prompt': 'fix'}], test_config(), output,
                               backend_factory=Backend, model_factory=Models(no_changes=True))
            update = read_json(output / 'example/update.json')
            self.assertEqual(update['decision'], 'no_changes')
            self.assertEqual(update['before_skill_version'], update['after_skill_version'])

    def test_wrong_model_routes_and_removed_reflection_config_are_rejected(self):
        import yaml
        source = yaml.safe_load((ROOT / 'configs/agent/qwen.yaml').read_text())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.yaml'
            for role, name in (('judge_model', 'qwen3.8-flash'), ('skill_error_model', 'qwen3.8-flash')):
                config = copy.deepcopy(source)
                config[role]['name'] = name
                path.write_text(yaml.safe_dump(config))
                with self.assertRaises(ValueError):
                    load_config(path)
            source['reflection_enabled'] = False
            path.write_text(yaml.safe_dump(source))
            with self.assertRaisesRegex(ValueError, 'Unsupported'):
                load_config(path)


if __name__ == '__main__':
    unittest.main()
