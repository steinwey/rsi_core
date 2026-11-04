import copy
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from rsi import UPSTREAM
from rsi.agent.action_protocol import GuardedAgent
from rsi.agent.evidence import RecordingEnvironment


class TimeoutEnvironment:
    def __init__(self, output, stderr=None):
        self.commands = []
        self.error = subprocess.TimeoutExpired('probe', 180, output=output, stderr=stderr)

    def execute(self, command, cwd=''):
        self.commands.append(command)
        if command == 'probe':
            raise self.error
        return {'output': 'COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\n', 'returncode': 0}

    def get_template_vars(self):
        return {}


class TimeoutOutputTests(unittest.TestCase):
    def test_long_timeout_preserves_raw_logs_and_exception_details(self):
        for output in (b'HEAD' + b'x' * 20000 + b'TAIL', '首' + '中' * 12000 + '尾'):
            with self.subTest(output_type=type(output)), tempfile.TemporaryDirectory() as directory:
                base = TimeoutEnvironment(output, stderr=b'original stderr')
                env = RecordingEnvironment(base, directory, lambda: 3)
                with self.assertRaises(subprocess.TimeoutExpired) as caught:
                    env.execute('probe', cwd='/testbed')
                self.assertIs(caught.exception, base.error)
                self.assertEqual((base.error.cmd, base.error.timeout, base.error.stderr),
                                 ('probe', 180, b'original stderr'))
                raw = output if isinstance(output, bytes) else output.encode('utf-8')
                text = raw.decode('utf-8')
                saved = Path(directory) / 'commands'
                record = json.loads((saved / '000001.json').read_text(encoding='utf-8'))
                self.assertEqual((saved / record['output']['path']).read_bytes(), raw)
                self.assertEqual(record['output']['bytes'], len(raw))
                self.assertEqual(record['output']['sha256'], hashlib.sha256(raw).hexdigest())
                self.assertEqual((saved / record['stderr']['path']).read_bytes(), b'original stderr')
                self.assertEqual(record['assistant_message'], 3)
                self.assertEqual(record['cwd'], '/testbed')
                visible = caught.exception.output.decode('utf-8')
                self.assertTrue(visible.startswith(text[:5000]))
                self.assertTrue(visible.endswith(text[-5000:]))
                self.assertIn(f'{len(text) - 10000} characters elided', visible)
                self.assertLess(len(visible), 10100)

    def test_short_timeout_outputs_are_unchanged(self):
        for output in (None, b'', b'short\xff', 'short', b'x' * 9999, '中' * 9999):
            with self.subTest(output_type=type(output)), tempfile.TemporaryDirectory() as directory:
                base = TimeoutEnvironment(output)
                env = RecordingEnvironment(base, directory, lambda: 0)
                with self.assertRaises(subprocess.TimeoutExpired) as caught:
                    env.execute('probe')
                self.assertIs(caught.exception.output, output)

    def test_success_and_other_errors_keep_existing_behavior(self):
        result = {'returncode': 0, 'output': 'x' * 20000}
        class Environment:
            def execute(self, command, cwd=''):
                if command == 'error':
                    raise error
                return result
        error = RuntimeError('other error')
        error.output = b'x' * 20000
        with tempfile.TemporaryDirectory() as directory:
            env = RecordingEnvironment(Environment(), directory, lambda: 0)
            self.assertIs(env.execute('success'), result)
            with self.assertRaises(RuntimeError) as caught:
                env.execute('error')
            self.assertIs(caught.exception, error)
            self.assertEqual(error.output, b'x' * 20000)

    def test_agent_can_continue_after_timeout_without_extra_calls(self):
        class Model:
            def __init__(self):
                self.n_calls = self.cost = 0
                self.requests = []
            def query(self, messages):
                self.requests.append(copy.deepcopy(messages))
                self.n_calls += 1
                command = 'probe' if self.n_calls == 1 else 'submit'
                return {'content': f'```bash\n{command}\n```'}
            def get_template_vars(self):
                return {}
        model = Model()
        # Match the failed run's output size without using Docker or an API.
        raw = b'HEAD\n' + b'x' * 17839914 + b'\nTAIL'
        with tempfile.TemporaryDirectory() as directory:
            base = TimeoutEnvironment(raw)
            env = RecordingEnvironment(base, directory, lambda: len(agent.messages) - 1)
            settings = yaml.safe_load((UPSTREAM / 'third_party/src/minisweagent/config/extra/swebench.yaml')
                                      .read_text(encoding='utf-8'))['agent']
            settings.update(step_limit=2, cost_limit=0)
            agent = GuardedAgent(model, env, **settings)
            self.assertEqual(agent.run('fix')[0], 'Submitted')
            self.assertEqual(base.commands, ['probe', 'submit'])
            self.assertEqual(agent.budget_calls, 2)
            self.assertEqual(agent.repair_calls, 0)
            self.assertEqual(env.count, 2)
            observation = model.requests[1][-1]['content']
            self.assertIn('timed out', observation)
            self.assertIn(f'{len(raw) - 10000} characters elided', observation)
            self.assertLess(len(json.dumps(model.requests[1]).encode('utf-8')), 16777216)
            self.assertEqual((Path(directory) / 'commands/000001.output.bin').read_bytes(), raw)


if __name__ == '__main__':
    unittest.main()
