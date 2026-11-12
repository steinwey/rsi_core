import json
import tempfile
import unittest
from pathlib import Path

import yaml

from rsi import ROOT
from rsi.agent.action_protocol import GuardedAgent
from rsi.agent.evidence import RecordingEnvironment
from rsi.agent.pathology_session import STALL_NOTE, open_pathology
from rsi.core.common import read_json
from rsi.core.config import load_config
from rsi.skills.trace2skill.evidence import EvidenceReader
from rsi.skills.trace2skill.pathology import hard_stop_for, trajectory_messages


def bash(text):
    return f'```bash\n{text}\n```'


SUBMIT = 'echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT'


class Box:
    def __init__(self):
        self.commands = []

    def execute(self, command, cwd=''):
        self.commands.append(command)
        if 'COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT' in command:
            return {'output': 'COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\n', 'returncode': 0}
        return {'output': 'ok', 'returncode': 0}

    def get_template_vars(self):
        return {}


class Model:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.n_calls = self.cost = 0
        self.last_finish_reason = None

    def query(self, messages, **kwargs):
        value = next(self.replies)
        self.n_calls += 1
        text, self.last_finish_reason = value if isinstance(value, tuple) else (value, 'stop')
        return {'content': text}

    def get_template_vars(self):
        return {}


def run_case(folder, replies, *, mode, k1=3, hard_stop=None, model='deepseek-v4.1-flash',
             step_limit=100, format_limit=5):
    config = {'pathology': {'mode': mode, 'k1': k1, 'hard_stop': hard_stop},
              'model': {'name': model}, 'step_limit': step_limit}
    session = open_pathology(config, folder)
    inner = Box()
    holder = {}

    def message_index():
        return len(holder['agent'].messages) - 1

    env = RecordingEnvironment(inner, folder, message_index)
    if session is not None:
        env.after_output = session.after_output
    agent = GuardedAgent(Model(replies), env, step_limit=20, cost_limit=0,
                         action_format_limit=format_limit, pathology=session)
    holder['agent'] = agent
    status, _ = agent.run('task')
    if session is not None:
        session.write()
    return status, agent, inner, session


class RuntimeTests(unittest.TestCase):
    def test_default_config_is_off_and_unknown_models_do_not_hard_stop(self):
        config = load_config(ROOT / 'configs/agent/qwen.yaml')
        self.assertEqual(config['pathology'], {'mode': 'off', 'k1': 3, 'hard_stop': None})
        self.assertIsNone(open_pathology(config, '.'))
        self.assertEqual(hard_stop_for('qwen3.8-flash', None), 20)
        self.assertEqual(hard_stop_for('deepseek-v4.1-flash', None), 8)
        self.assertIsNone(hard_stop_for('unknown-model', None))
        source = yaml.safe_load((ROOT / 'configs/agent/qwen.yaml').read_text(encoding='utf-8'))
        source['pathology'] = {'mode': 'block'}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.yaml'
            path.write_text(yaml.safe_dump(source), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'pathology.mode'):
                load_config(path)

    def test_off_executes_every_command_and_writes_no_live_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            status, agent, inner, session = run_case(
                folder, [bash('grep x'), bash('grep x'), bash('grep x'), bash(SUBMIT)], mode='off', k1=2)
            self.assertEqual(status, 'Submitted')
            self.assertIsNone(session)
            self.assertEqual(inner.commands, ['grep x', 'grep x', 'grep x', SUBMIT])
            self.assertFalse(any('[monitor]' in message.get('content', '') for message in agent.messages))
            self.assertFalse(any('monitor_note' in message for message in agent.messages))
            self.assertFalse((folder / 'pathology-live.json').exists())

    def test_shadow_logs_without_changing_the_observation(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            status, agent, inner, session = run_case(
                folder, [bash('grep x'), bash('grep x'), bash('grep x'), bash(SUBMIT)], mode='shadow', k1=2)
            self.assertEqual(status, 'Submitted')
            self.assertEqual(inner.commands, ['grep x', 'grep x', 'grep x', SUBMIT])
            self.assertFalse(any('[monitor]' in message.get('content', '') for message in agent.messages))
            live = read_json(folder / 'pathology-live.json')
            self.assertEqual(live['mode'], 'shadow')
            self.assertEqual(live['thresholds']['hard_stop'], 8)
            repeats = [event for event in live['events'] if event['type'] == 'repeated_command']
            self.assertEqual([event['action'] for event in repeats], ['none', 'none'])
            self.assertEqual([event['count'] for event in repeats], [2, 3])

    def test_advise_appends_one_updating_line_outside_the_raw_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rollout = root / 'rollout-0'
            status, agent, inner, _ = run_case(
                rollout, [bash('grep x'), bash('grep x'), bash('grep x'), bash(SUBMIT)], mode='advise', k1=2)
            self.assertEqual(status, 'Submitted')
            self.assertEqual(inner.commands, ['grep x', 'grep x', 'grep x', SUBMIT])
            notes = [message for message in agent.messages if message.get('monitor_note')]
            self.assertEqual(len(notes), 2)
            self.assertEqual(notes[0]['monitor_note'].count('[monitor]'), 1)
            self.assertIn('identical output 2 times', notes[0]['monitor_note'])
            self.assertIn('identical output 3 times', notes[1]['monitor_note'])
            self.assertNotIn('identical output 2 times', notes[1]['monitor_note'])
            self.assertTrue(notes[0]['content'].endswith('\n' + notes[0]['monitor_note']))
            raw = (rollout / 'commands' / '000002.output.txt').read_text(encoding='utf-8')
            self.assertEqual(raw, 'ok')
            self.assertNotIn('[monitor]', raw)
            record = read_json(rollout / 'commands' / '000002.json')
            self.assertEqual(record['monitor_note'], notes[0]['monitor_note'])
            cleaned = trajectory_messages({'messages': agent.messages})
            index = agent.messages.index(notes[0])
            self.assertNotIn('[monitor]', cleaned[index]['content'])
            self.assertNotIn('monitor_note', cleaned[index])
            self.assertIn('[monitor]', agent.messages[index]['content'])
            reader = EvidenceReader({'benchmark': 'terminal'},
                                    {'rollout': 0, 'messages': agent.messages, 'artifact': ''}, {},
                                    root / 'trace2skill' / 'rollout-0')
            self.assertNotIn('[monitor]', reader.read_evidence('trajectory'))
            command = json.loads(reader.read_evidence('command:2'))
            self.assertEqual(command['text'], 'ok')

    def test_enforce_skips_the_sandbox_and_the_rollout_continues(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            status, agent, inner, _ = run_case(
                folder, [bash('grep x'), bash('grep x'), bash('grep x'), bash('grep x'), bash(SUBMIT)],
                mode='enforce', k1=2, hard_stop=3)
            self.assertEqual(status, 'Submitted')
            self.assertEqual(inner.commands, ['grep x', 'grep x', SUBMIT])
            skips = [message['monitor_note'] for message in agent.messages
                     if str(message.get('monitor_note', '')).startswith('[monitor] Skipped')]
            self.assertEqual(len(skips), 2)
            self.assertIn('identical output 2 times', skips[0])
            self.assertIn('Last output summary: ok', skips[0])
            self.assertIn('identical output 3 times', skips[1])
            raw = (folder / 'commands' / '000003.output.txt').read_text(encoding='utf-8')
            self.assertEqual(raw, '')
            self.assertNotIn('[monitor]', raw)
            live = read_json(folder / 'pathology-live.json')
            self.assertEqual([event['action'] for event in live['events'] if event['type'] == 'repeated_command'],
                             ['advise', 'skip', 'skip'])
            cleaned = trajectory_messages({'messages': agent.messages})
            self.assertTrue(all('[monitor]' not in message.get('content', '') for message in cleaned))

    def test_unknown_model_enforce_advises_and_does_not_skip(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            _, _, inner, session = run_case(
                folder, [bash('grep x'), bash('grep x'), bash('grep x'), bash(SUBMIT)],
                mode='enforce', k1=2, model='unknown-model')
            self.assertEqual(inner.commands, ['grep x', 'grep x', 'grep x', SUBMIT])
            self.assertIsNone(session.resolved_hard_stop)
            self.assertTrue(any(event['action'] == 'advise' for event in session.events))
            self.assertFalse(any(event['action'] == 'skip' for event in session.events))

    def test_stall_hint_is_injected_once_and_format_runs_are_only_logged(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            replies = [bash('grep x'), bash('grep x'), bash('grep x'), bash('grep y'), bash(SUBMIT)]
            _, agent, _, session = run_case(folder, replies, mode='advise', k1=9, step_limit=4)
            stall_messages = [message for message in agent.messages if 'minimal implementation' in message.get('content', '')]
            self.assertEqual(len(stall_messages), 1)
            self.assertIn(STALL_NOTE, stall_messages[0]['content'])
            self.assertNotIn('MUST', stall_messages[0]['content'])
            self.assertEqual(sum(event['type'] == 'zero_edit_stall' and event['action'] == 'advise'
                                 for event in session.events), 1)
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            truncated = [(bash('grep x'), 'length')] * 3
            _, agent, inner, session = run_case(folder, [*truncated, bash('grep y'), bash(SUBMIT)], mode='enforce', k1=2)
            self.assertEqual(inner.commands, ['grep y', SUBMIT])
            self.assertFalse(any('[monitor]' in message.get('content', '') for message in agent.messages))
            logged = [event for event in session.events if event['type'] == 'truncation_loop']
            self.assertEqual([(event['count'], event['action']) for event in logged], [(3, 'none')])


if __name__ == '__main__':
    unittest.main()
