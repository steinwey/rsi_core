import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rsi.agent.action_protocol import GuardedAgent, validate_action_protocol
from rsi.agent.runner import run_experiment
from rsi.core.common import read_json
from rsi.core.config import load_config
from rsi import ROOT
from test_core_baseline import Backend, Models
from scripts.monitor_experiment import problem_in, activity_paths
import threading


class SequenceModel:
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


class Environment:
    def __init__(self):
        self.commands = []

    def get_template_vars(self):
        return {}

    def execute(self, command):
        self.commands.append(command)
        return {'output': 'COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT' if command == 'submit' else 'ok', 'returncode': 0}


def command(text):
    return f'```bash\n{text}\n```'


_DSML = '\uff5cDSML\uff5c'


def dsml(script, *, invoke='bash', parameter='command', wrapped=True):
    body = (f'<{_DSML} invoke name="{invoke}">\n'
            f'<{_DSML} parameter name="{parameter}" string="true">{script}</{_DSML} parameter>\n'
            f'</{_DSML} invoke>')
    if wrapped:
        body = f'<{_DSML} calls>\n{body}\n</{_DSML} calls>'
    return body


class ActionProtocolTests(unittest.TestCase):
    def agent(self, replies, **kwargs):
        env = Environment()
        return GuardedAgent(SequenceModel(replies), env, step_limit=20, cost_limit=0, **kwargs), env

    def test_two_failures_then_submission_executes_only_valid_command(self):
        agent, env = self.agent(['', '```bash\necho unfinished', command('submit')])
        status, _ = agent.run('task')
        self.assertEqual(status, 'Submitted')
        self.assertEqual(env.commands, ['submit'])
        self.assertEqual(agent.format_consecutive, 0)
        self.assertEqual(agent.format_total, 2)
        self.assertEqual([e['executed'] for e in agent.action_events], [False, False, True])

    def test_every_invalid_kind_is_bounded_and_never_executes(self):
        cases = [('', 'empty_response'), (command(''), 'empty_command'),
                 (command('one') + '\n' + command('two'), 'multiple_action_blocks'),
                 ('```bash\necho bad\n</tool_call>', 'mixed_tool_protocol'),
                 ('```bash\necho unclosed', 'missing_or_unclosed_bash_block'),
                 ((command('echo cut'), 'length'), 'output_truncated'),
                 (command('echo ok') + '\n```python\nunfinished', 'extra_unclosed_code_block')]
        for value, error in cases:
            with self.subTest(error=error):
                agent, env = self.agent([value] * 3)
                status, feedback = agent.run('task')
                self.assertEqual(status, 'ActionFormatFailed')
                self.assertIn(error, feedback)
                self.assertEqual(agent.model.n_calls, 3)
                self.assertEqual(env.commands, [])
                self.assertTrue(all(e['error_type'] == error for e in agent.action_events))

    def test_success_resets_consecutive_counter_and_xml_inside_command_is_allowed(self):
        valid = "printf '%s' '<tool_call>data</tool_call>'"
        agent, env = self.agent(['bad', 'bad', command(valid), 'bad', 'bad', command('submit')])
        self.assertEqual(agent.run('task')[0], 'Submitted')
        self.assertEqual(agent.format_total, 4)
        self.assertEqual(env.commands, [valid, 'submit'])

    def test_step_budget_is_not_extended_by_format_retries(self):
        agent, env = self.agent(['bad'] * 5, action_format_limit=10)
        agent.config.step_limit = 2
        self.assertEqual(agent.run('task')[0], 'LimitsExceeded')
        self.assertEqual(agent.model.n_calls, 2)

    def test_zero_step_limit_allows_trajectory_beyond_100_calls(self):
        from rsi.agent.execution import solver_step_limit_note
        agent, env = self.agent([command('true')] * 120 + [command('submit')])
        agent.config.step_limit = 0
        self.assertEqual(agent.run('task')[0], 'Submitted')
        self.assertEqual(agent.model.n_calls, 121)
        self.assertEqual(len(env.commands), 121)
        self.assertIn('no fixed step', solver_step_limit_note(0))
        self.assertNotIn('hard limit of 0', solver_step_limit_note(0))

    def test_invalid_limit(self):
        for limit in (True, 0, -1, 1.5):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                validate_action_protocol({'action_format_max_consecutive': limit})

    def test_llm_repair_receives_error_and_fix_then_executes_unchanged_command(self):
        requests = []
        def repair(data):
            requests.append(data)
            return {'repairable': True, 'content': command('submit'), 'reason': 'Replace trailing XML with a bash fence'}, 'stop'
        agent, env = self.agent(['```bash\nsubmit\n</parameter>'], action_repair=repair)
        self.assertEqual(agent.run('task')[0], 'Submitted')
        self.assertEqual(env.commands, ['submit'])
        self.assertEqual(requests[0]['error_type'], 'mixed_tool_protocol')
        self.assertIn('preserving', requests[0]['required_fix'])
        self.assertEqual(agent.repair_calls, 1)
        self.assertEqual(agent.format_consecutive, 0)

    def test_changed_command_refusal_bad_json_and_invalid_repair_never_execute(self):
        replies = [({'repairable': True, 'content': command('different'), 'reason': 'changed'}, 'stop'),
                   ({'repairable': False, 'content': '', 'reason': 'Ambiguous'}, 'stop'),
                   ({'repairable': True, 'content': 'no fence', 'reason': 'bad'}, 'stop'),
                   ({'content': command('submit')}, 'stop'),
                   ({'repairable': True, 'content': command('submit'), 'reason': 'cut'}, 'length')]
        for reply in replies:
            with self.subTest(reply=reply):
                agent, env = self.agent(['```bash\nsubmit\n</parameter>'] * 3, action_repair=lambda _: reply)
                self.assertEqual(agent.run('task')[0], 'ActionFormatFailed')
                self.assertEqual(env.commands, [])
                self.assertEqual(agent.repair_calls, 3)
                self.assertEqual(agent.format_total, 3)

    def test_unrecoverable_input_does_not_call_repair_model(self):
        def repair(_):
            self.fail('Do not ask a model to invent missing commands')
        agent, env = self.agent([''] * 3, action_repair=repair)
        self.assertEqual(agent.run('task')[0], 'ActionFormatFailed')
        self.assertEqual(agent.repair_calls, 0)
        self.assertEqual(env.commands, [])

    def test_single_multicommand_script_with_heredoc_can_be_rewrapped(self):
        script = "cd /testbed && python - <<'PYEOF'\nprint('unchanged')\nPYEOF\npython -c \"print('syntax check')\""
        agent, env = self.agent(['```bash\n' + script + '\n</parameter>\n</function>\n</tool_call>', command('submit')],
            action_repair=lambda _: ({'repairable': True, 'content': command(script),
                                      'reason': 'Close outer fence only; preserve both commands'}, 'stop'))
        self.assertEqual(agent.run('task')[0], 'Submitted')
        self.assertEqual(env.commands, [script, 'submit'])
        self.assertEqual(agent.repair_calls, 1)

    def test_repair_must_not_drop_or_reorder_commands_in_one_script(self):
        script = 'echo first\necho second'
        for changed in ('echo first', 'echo second\necho first', 'echo first && echo second'):
            with self.subTest(changed=changed):
                agent, env = self.agent(['```bash\n' + script + '\n</parameter>'] * 3,
                    action_repair=lambda _: ({'repairable': True, 'content': command(changed), 'reason': 'Rewrap'}, 'stop'))
                self.assertEqual(agent.run('task')[0], 'ActionFormatFailed')
                self.assertEqual(env.commands, [])

    def test_independent_blocks_are_not_sent_for_merging(self):
        def repair(_):
            self.fail('Multiple independent blocks must not be merged by an LLM')
        agent, env = self.agent([(command('echo first') + '\n' + command('echo second'))] * 3,
                                action_repair=repair)
        self.assertEqual(agent.run('task')[0], 'ActionFormatFailed')
        self.assertEqual(env.commands, [])
        self.assertEqual(agent.repair_calls, 0)

    def test_unique_dsml_command_executes_without_repair(self):
        script = "cd /testbed && sed -n '680,720p' src/click/core.py && echo \"---\" && grep -rn \"UNSET\" src/click/*.py"
        def repair(_):
            self.fail('A unique DSML bash command must execute without a repair call')
        reply = 'THOUGHT: Inspect lookup_default.\n\n' + dsml(script)
        agent, env = self.agent([reply, command('submit')], action_repair=repair)
        self.assertEqual(agent.run('task')[0], 'Submitted')
        self.assertEqual(env.commands, [script, 'submit'])
        self.assertEqual(agent.repair_calls, 0)
        self.assertEqual(agent.format_total, 0)

    def test_dsml_without_calls_wrapper_preserves_multiline_script(self):
        script = "cd /testbed && python - <<'PY'\nprint('unchanged')\nPY"
        agent, env = self.agent([dsml(script, wrapped=False), command('submit')])
        self.assertEqual(agent.run('task')[0], 'Submitted')
        self.assertEqual(env.commands, [script, 'submit'])

    def test_ambiguous_or_incomplete_dsml_never_executes_or_repairs(self):
        second = dsml('echo second', wrapped=False)
        cases = {
            'two_invokes': dsml('echo first') + '\n' + second,
            'two_parameters': (f'<{_DSML} invoke name="bash">\n'
                               f'<{_DSML} parameter name="command" string="true">echo first</{_DSML} parameter>\n'
                               f'<{_DSML} parameter name="command" string="true">echo second</{_DSML} parameter>\n'
                               f'</{_DSML} invoke>'),
            'unclosed': dsml('echo cut').replace(f'</{_DSML} calls>', ''),
            'empty_command': dsml('   '),
            'wrong_tool': dsml('echo nope', invoke='python'),
            'wrong_parameter': dsml('echo nope', parameter='cmd'),
            'extra_fence': dsml('echo one') + '\n' + command('echo two'),
            'nested_marker': dsml(f'echo <{_DSML} invoke>'),
        }
        expected = {'two_invokes': 'multiple_action_blocks', 'two_parameters': 'multiple_action_blocks',
                    'extra_fence': 'multiple_action_blocks'}
        for name, reply in cases.items():
            with self.subTest(name=name):
                def repair(_):
                    self.fail('Incomplete or ambiguous DSML must not be repaired')
                agent, env = self.agent([reply] * 3, action_repair=repair)
                status, feedback = agent.run('task')
                self.assertEqual(status, 'ActionFormatFailed')
                self.assertIn(expected.get(name, 'incomplete_dsml'), feedback)
                self.assertEqual(env.commands, [])
                self.assertEqual(agent.repair_calls, 0)

    def test_bash_fence_with_trailing_dsml_closers_still_executes(self):
        reply = command('submit') + f'\n</{_DSML} parameter>\n</{_DSML} invoke>\n</{_DSML} calls>'
        agent, env = self.agent([reply])
        self.assertEqual(agent.run('task')[0], 'Submitted')
        self.assertEqual(env.commands, ['submit'])

    def test_truncated_dsml_is_not_executed(self):
        agent, env = self.agent([(dsml('echo partial'), 'length')] * 3)
        status, feedback = agent.run('task')
        self.assertEqual(status, 'ActionFormatFailed')
        self.assertIn('output_truncated', feedback)
        self.assertEqual(env.commands, [])

    def test_monitor_accepts_bounded_format_stop_and_tracks_its_logs(self):
        route = {'status': 'ActionFormatFailed', 'messages': [{'role': 'assistant', 'content': ''}] * 3}
        self.assertIsNone(problem_in(Path('rollout-0/trajectory.json'), route))
        with tempfile.TemporaryDirectory() as directory:
            active = Path(directory)
            path = active / 'rollout-0/action-protocol.jsonl'
            path.parent.mkdir()
            path.write_text('{}\n')
            self.assertIn(path, activity_paths(active))


class FormatModels(Models):
    def __call__(self, config):
        client = super().__call__(config)
        query = client.query
        def respond(messages, **kwargs):
            if config['name'] == 'solver':
                client.n_calls += 1
                return {'content': '```bash\necho not executed\n</tool_call>'}
            return query(messages, **kwargs)
        client.query = respond
        return client


class ActionProtocolIntegrationTests(unittest.TestCase):
    def config(self):
        config = load_config(ROOT / 'configs/agent/qwen.yaml')
        config['action_format_repair_enabled'] = False
        for key, name in (('model', 'solver'), ('judge_model', 'judge'), ('skill_editor_model', 'judge'), ('skill_error_model', 'error_analyst'),
                          ('skill_success_model', 'success_analyst')):
            config[key]['name'] = name
        Backend.barrier, Backend.instances = threading.Barrier(5), []
        return config

    def test_initial_format_failure_keeps_patches_and_commits_scored_task(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'run'
            with patch('rsi.agent.runner.functional_gate', return_value={'passed': True}):
                reports = run_experiment([{'id': 'task', 'benchmark': 'swe', 'prompt': 'fix'}], self.config(), output,
                                         backend_factory=Backend, model_factory=FormatModels())
            self.assertTrue(reports[0]['supervision_complete'])
            for i in range(5):
                route = read_json(output / f'task/rollout-{i}/trajectory.json')
                self.assertEqual(route['status'], 'ActionFormatFailed')
                self.assertNotIn('reflection', route)
                self.assertEqual(route['action_protocol']['total_failures'], 3)
                self.assertTrue(route['artifact'])
                self.assertFalse((output / f'task/rollout-{i}/reflection').exists())
            self.assertEqual(len(read_json(output / 'checkpoint.json')['completed']), 1)

    def test_all_empty_patches_prevent_scoring_and_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'run'
            config = self.config()
            config['evolve'] = False
            with patch('rsi.agent.runner.functional_gate', return_value={'passed': True}), \
                 patch.object(Backend, 'artifact', return_value=''), \
                 patch.object(Backend, 'score') as score, self.assertRaisesRegex(RuntimeError, 'No route'):
                run_experiment([{'id': 'task', 'benchmark': 'swe', 'prompt': 'fix'}], config, output,
                               backend_factory=Backend, model_factory=FormatModels())
            score.assert_not_called()
            self.assertEqual(read_json(output / 'checkpoint.json')['completed'], [])
            self.assertTrue(all(b.closed for b in Backend.instances))

    def test_repair_usage_and_requests_are_saved_in_real_execution_path(self):
        class RepairModels(Models):
            def __call__(self, config):
                client = super().__call__(config)
                query = client.query
                def respond(messages, **kwargs):
                    if config['name'] == 'solver':
                        client.n_calls += 1
                        return {'content': '```bash\necho COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\n</parameter>'}
                    if messages[0]['content'].startswith('Repair only the action protocol'):
                        import json
                        client.n_calls += 1
                        data = json.loads(messages[1]['content'])
                        assert data['error_type'] == 'mixed_tool_protocol'
                        assert data['required_fix']
                        return {'content': json.dumps({'repairable': True,
                            'content': command('echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT'), 'reason': 'Fix closing fence only'})}
                    return query(messages, **kwargs)
                client.query = respond
                return client
        config = self.config()
        config['action_format_repair_enabled'] = True
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'run'
            with patch('rsi.agent.runner.functional_gate', return_value={'passed': True}):
                report = run_experiment([{'id': 'task', 'benchmark': 'swe', 'prompt': 'fix'}], config, output,
                                       backend_factory=Backend, model_factory=RepairModels())[0]
            self.assertEqual(report['input_tokens'], 220)
            self.assertEqual(report['output_tokens'], 110)
            for i in range(5):
                route = read_json(output / f'task/rollout-{i}/trajectory.json')
                self.assertEqual(route['status'], 'Submitted')
                self.assertEqual(route['action_repair_usage']['calls'], 1)
                self.assertTrue((output / f'task/rollout-{i}/action-repair/call-1.request.json').exists())
                self.assertTrue(any(m.get('action_format_repair') for m in route['messages']))
