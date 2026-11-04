import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

os.environ.setdefault('LITELLM_LOCAL_MODEL_COST_MAP', 'True')
import litellm
from litellm.types.utils import ModelResponseStream

from rsi.agent.action_protocol import GuardedAgent
from rsi.agent.runner import run_experiment
from rsi.core.common import read_json
from rsi.core.model import Model, IncompleteStreamError, RequestDeadlineExceeded
from rsi.core.responses import generate_object
from test_core_baseline import Backend, Environment, test_config


def chunk(content=None, reasoning=None, finish=None, usage=None, **fields):
    delta = {}
    if content is not None:
        delta['content'] = content
    if reasoning is not None:
        delta['reasoning_content'] = reasoning
    return ModelResponseStream(
        model='qwen3.8-max',
        choices=[] if usage is not None and not delta and finish is None else
                [{'index': 0, 'delta': delta, 'finish_reason': finish, **fields}], usage=usage)


USAGE = {'prompt_tokens': 20, 'completion_tokens': 8, 'total_tokens': 28,
         'completion_tokens_details': {'reasoning_tokens': 5}}


class Stream:
    def __init__(self, items):
        self.items = items
        self.closed = False

    def __iter__(self):
        for item in self.items:
            if isinstance(item, BaseException):
                raise item
            yield item

    def close(self):
        self.closed = True


def success(content='```bash\necho COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\n```', finish='stop'):
    return Stream([chunk(reasoning='think'), chunk(content=content, finish=finish), chunk(usage=USAGE)])


class ThinkingStreamTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.log = Path(self.temp.name) / 'model-stream.jsonl'
        cost = patch('rsi.core.model.recorded_cost', return_value=None)
        cost.start()
        self.addCleanup(cost.stop)

    def model(self, **options):
        client = Model({'name': 'qwen3.8-max', 'api_key': 'test-only',
                        'api_base': 'https://example.invalid/v1', 'custom_llm_provider': 'openai',
                        'extra_body': {'enable_thinking': True}, 'num_retries': 0, 'timeout': 120,
                        **options})
        client.stream_log = self.log
        return client

    def events(self):
        return [json.loads(line) for line in self.log.read_text(encoding='utf-8').splitlines()]

    def test_disabled_thinking_keeps_nonstreaming_request_and_reply(self):
        client = self.model(extra_body={'enable_thinking': False})
        reply = litellm.ModelResponse(model='qwen3.8-max',
            choices=[{'message': {'role': 'assistant', 'content': 'ok'}, 'finish_reason': 'stop'}], usage=USAGE)
        messages = [{'role': 'user', 'content': 'test', 'usage': {'calls': 9}}]
        with patch('litellm.completion', return_value=reply) as call:
            result = client.query(messages)
        self.assertNotIn('stream', call.call_args.kwargs)
        self.assertNotIn('stream_options', call.call_args.kwargs)
        self.assertEqual(call.call_args.kwargs['messages'], [{'role': 'user', 'content': 'test'}])
        self.assertIn('usage', messages[0])
        self.assertEqual(set(result), {'content', 'usage'})
        self.assertEqual(result['content'], 'ok')
        self.assertFalse(self.log.exists())

    def test_stream_assembles_action_and_preserves_exact_reasoning_with_final_usage(self):
        client = self.model()
        source = Stream([chunk(reasoning='想'), chunk(reasoning=' '), chunk(reasoning='一下'),
                        chunk(content='```bash\necho '), chunk(content='ok\n```', finish='stop'), chunk(usage=USAGE)])
        with patch('litellm.completion', return_value=source) as call:
            result = client.query([{'role': 'user', 'content': 'test'}])
        self.assertTrue(call.call_args.kwargs['stream'])
        self.assertEqual(call.call_args.kwargs['stream_options'], {'include_usage': True})
        self.assertEqual(result['content'], '```bash\necho ok\n```')
        self.assertEqual(set(result), {'content', 'usage'})
        self.assertEqual((client.n_calls, client.input_tokens, client.output_tokens, client.reasoning_tokens), (1, 20, 8, 5))
        self.assertEqual(client.last_reasoning_chars, 4)
        self.assertEqual(client.last_finish_reason, 'stop')
        events = self.events()
        self.assertEqual(''.join(e['text'] for e in events if e['event'] == 'reasoning_delta'), '想 一下')
        self.assertEqual(events[-1]['event'], 'complete')
        self.assertNotIn('test-only', self.log.read_text())
        self.assertTrue(source.closed)

    def test_deltas_are_flushed_before_the_response_completes(self):
        owner = self
        class LiveStream(Stream):
            def __iter__(self):
                yield chunk(reasoning='partial thought')
                saved = owner.events()
                owner.assertEqual(saved[-1]['event'], 'reasoning_delta')
                owner.assertFalse(any(e['event'] == 'complete' for e in saved))
                yield chunk(content='ok', finish='stop')
                yield chunk(usage=USAGE)
        with patch('litellm.completion', return_value=LiveStream([])):
            self.model().query([{'role': 'user', 'content': 'test'}])

    def test_partial_attempt_is_archived_but_never_executed_or_added_to_history(self):
        client = self.model(num_retries=2)
        failed = Stream([chunk(reasoning='failed thought'), chunk(content='```bash\necho BAD\n```'), TimeoutError('timed out')])
        good = success()
        with patch('litellm.completion', side_effect=[failed, good]), patch('rsi.core.model.time.sleep'):
            env = Environment()
            with patch.object(env, 'execute', wraps=env.execute) as execute:
                agent = GuardedAgent(client, env, step_limit=1, cost_limit=0)
                self.assertEqual(agent.run('test')[0], 'Submitted')
        self.assertEqual([c.args[0] for c in execute.call_args_list], ['echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT'])
        self.assertEqual(client.n_calls, 1)
        self.assertEqual(client.output_tokens, 8)
        self.assertNotIn('failed thought', json.dumps(agent.messages))
        self.assertNotIn('echo BAD', json.dumps(agent.messages))
        events = self.events()
        self.assertEqual([e['attempt'] for e in events if e['event'] == 'start'], [1, 2])
        self.assertEqual([e['event'] for e in events if e['event'] in ('complete', 'error')], ['error', 'complete'])
        self.assertTrue(failed.closed and good.closed)

    def test_missing_finish_or_usage_is_not_a_successful_call(self):
        for items in ([chunk(content='ok'), chunk(usage=USAGE)],
                      [chunk(content='ok', finish='stop')]):
            with self.subTest(items=items):
                client = self.model()
                source = Stream(items)
                with patch('litellm.completion', return_value=source), self.assertRaises(IncompleteStreamError):
                    client.query([{'role': 'user', 'content': 'test'}])
                self.assertEqual(client.n_calls, 0)
                self.assertTrue(source.closed)

    def test_final_provider_usage_is_counted_once_not_added_to_intermediate_usage(self):
        source = Stream([chunk(reasoning='think', usage={'prompt_tokens': 20, 'completion_tokens': 3}),
                         chunk(content='ok', finish='stop'), chunk(usage=USAGE)])
        client = self.model()
        with patch('litellm.completion', return_value=source):
            client.query([{'role': 'user', 'content': 'test'}])
        self.assertEqual((client.input_tokens, client.output_tokens, client.reasoning_tokens), (20, 8, 5))

    def test_deadline_interrupts_a_blocked_stream_without_retrying_or_counting_a_step(self):
        class Blocked(Stream):
            def __init__(self):
                super().__init__([])
                self.release = threading.Event()
            def __iter__(self):
                yield chunk(reasoning='still thinking')
                if not self.release.wait(2):
                    raise AssertionError('Deadline did not close blocked stream')
                raise ConnectionError('closed')
            def close(self):
                super().close()
                self.release.set()
        source = Blocked()
        client = self.model(num_retries=2)
        start = time.monotonic()
        with patch('litellm.completion', return_value=source) as call, self.assertRaises(RequestDeadlineExceeded):
            client.query([{'role': 'user', 'content': 'test'}], deadline=start + 0.05)
        self.assertLess(time.monotonic() - start, 1)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(client.n_calls, 0)
        self.assertEqual(self.events()[-1]['event'], 'error')
        self.assertTrue(source.closed)

    def test_litellm_wrapper_underlying_stream_is_closed(self):
        class Wrapper:
            def __init__(self):
                self.completion_stream = success()
            def __iter__(self):
                return iter(self.completion_stream)
        source = Wrapper()
        with patch('litellm.completion', return_value=source):
            self.model().query([{'role': 'user', 'content': 'test'}])
        self.assertTrue(source.completion_stream.closed)

    def test_deadline_also_applies_while_assembling_the_completed_stream(self):
        client = self.model()
        build = litellm.stream_chunk_builder
        def slow_build(*args, **kwargs):
            time.sleep(0.08)
            return build(*args, **kwargs)
        with patch('litellm.completion', return_value=success()), \
             patch('litellm.stream_chunk_builder', side_effect=slow_build), \
             self.assertRaises(RequestDeadlineExceeded):
            client.query([{'role': 'user', 'content': 'test'}], deadline=time.monotonic()+0.05)
        self.assertEqual(client.n_calls, 0)

    def test_sdk_synthetic_stop_and_estimated_usage_cannot_turn_incomplete_output_into_success(self):
        for finish, raw in ((None, [chunk(usage=USAGE)]), ('stop', [])):
            with self.subTest(provider_finish=finish):
                source = success()
                source.received_finish_reason = finish
                source.chunks = raw
                client = self.model()
                with patch('litellm.completion', return_value=source), self.assertRaises(IncompleteStreamError):
                    client.query([{'role': 'user', 'content': 'test'}])
                self.assertEqual(client.n_calls, 0)
                self.assertTrue(source.closed)

    def test_explicit_nonstreaming_thinking_also_archives_reasoning(self):
        reply = litellm.ModelResponse(model='qwen3.8-max', choices=[{
            'message': {'role': 'assistant', 'content': 'ok', 'reasoning_content': 'full thought'},
            'finish_reason': 'stop'}], usage=USAGE)
        client = self.model(stream=False)
        with patch('litellm.completion', return_value=reply) as call:
            self.assertEqual(client.query([{'role': 'user', 'content': 'test'}])['content'], 'ok')
        self.assertFalse(call.call_args.kwargs['stream'])
        self.assertNotIn('stream_options', call.call_args.kwargs)
        self.assertEqual([e['text'] for e in self.events() if e['event'] == 'reasoning_delta'], ['full thought'])

    def test_length_finish_still_rejects_the_action(self):
        client = self.model()
        with patch('litellm.completion', return_value=success(finish='length')):
            env = Environment()
            with patch.object(env, 'execute') as execute:
                agent = GuardedAgent(client, env, step_limit=1, cost_limit=0, action_format_limit=1)
                self.assertEqual(agent.run('test')[0], 'ActionFormatFailed')
                execute.assert_not_called()
        self.assertEqual(client.n_calls, 1)

    def test_json_generation_uses_its_own_log_and_restores_the_solver_log(self):
        client = self.model()
        folder = Path(self.temp.name) / 'repair'
        with patch('litellm.completion', return_value=success('{"ok":true}')):
            self.assertEqual(generate_object(client, 'repair', {}, folder, 'call-1'), {'ok': True})
        self.assertEqual(client.stream_log, self.log)
        self.assertTrue((folder/'call-1.stream.jsonl').exists())
        self.assertEqual(set(read_json(folder/'call-1.response.json')), {'content', 'usage'})

    def test_failed_stream_has_no_checkpoint_commit_and_resume_archives_its_log(self):
        config = test_config()
        config.update(evolve=False, rollouts=1, rollout_workers=1, execution_mode='single')
        config['model'].update(name='qwen3.8-max', extra_body={'enable_thinking': True}, num_retries=2)
        task = {'id': 'thinking-task', 'benchmark': 'swe', 'prompt': 'Fix issue'}
        output = Path(self.temp.name)/'run'
        Backend.barrier, Backend.instances = threading.Barrier(1), []
        def failed(*args, **kwargs):
            return Stream([chunk(reasoning='unfinished'), chunk(content='```bash\necho BAD\n```')])
        with patch('rsi.agent.runner.functional_gate', return_value={'passed': True}), \
             patch('litellm.completion', side_effect=failed) as call, \
             patch('rsi.core.model.time.sleep'), patch.dict(os.environ, {'QWEN_TOKEN_PLAN_API_KEY': 'test-only'}):
            with self.assertRaisesRegex(RuntimeError, 'API errors'):
                run_experiment([task], config, output, backend_factory=Backend)
            self.assertEqual(call.call_count, 3)
        self.assertEqual(read_json(output/'checkpoint.json')['completed'], [])
        route = read_json(output/'thinking-task/rollout-0/trajectory.json')
        self.assertEqual(route['executed_commands'], 0)
        self.assertEqual(route['usage']['calls'], 0)
        self.assertEqual(route['messages'][-1]['role'], 'user')
        log = output/'thinking-task/rollout-0/model-stream.jsonl'
        before = log.read_bytes()
        with patch('rsi.agent.runner.functional_gate', return_value={'passed': True}), \
             patch('litellm.completion', side_effect=lambda **kwargs: success()), \
             patch.dict(os.environ, {'QWEN_TOKEN_PLAN_API_KEY': 'test-only'}):
            reports = run_experiment([task], config, output, resume=True, backend_factory=Backend)
        self.assertTrue(reports[0]['success'])
        self.assertEqual(len(read_json(output/'checkpoint.json')['completed']), 1)
        archived = list((output/'attempts').glob('*/rollout-0/model-stream.jsonl'))
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0].read_bytes(), before)
        self.assertTrue(all(b.closed for b in Backend.instances))


if __name__ == '__main__':
    unittest.main()
