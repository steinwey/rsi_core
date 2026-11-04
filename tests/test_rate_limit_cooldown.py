import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from rsi import ROOT
from rsi.core import model as model_module
from rsi.core.config import load_config
from rsi.core.model import Model, RequestDeadlineExceeded, request_options


class RateLimitError(Exception):
    status_code = 429


class ServiceUnavailableError(Exception):
    status_code = 503


class FakeClock:
    def __init__(self):
        self.now = 100.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class RateLimitCooldownTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.completion = Mock()
        self.response = SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=2),
            choices=[SimpleNamespace(message=SimpleNamespace(content='ok'), finish_reason='stop')])
        patches = [
            patch.dict('sys.modules', {'litellm': SimpleNamespace(completion=self.completion)}),
            patch.object(model_module, 'recorded_cost', return_value=0),
            patch.object(model_module, '_RATE_LIMIT_COOLDOWNS', model_module._RateLimitCooldown()),
            patch.object(model_module.time, 'monotonic', self.clock.monotonic),
            patch.object(model_module.time, 'sleep', self.clock.sleep),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def client(self, **overrides):
        return Model({'name': 'qwen3.8-flash', 'api_base': 'https://example.invalid/v1',
                      'api_key': 'test-only', 'num_retries': 2, 'timeout': 120,
                      'rate_limit_retry_delay': 60, **overrides})

    def query(self, client=None, **kwargs):
        return (client or self.client()).query([{'role': 'user', 'content': 'test'}], **kwargs)

    def test_rate_limit_waits_sixty_seconds_without_forwarding_local_option(self):
        self.completion.side_effect = [RateLimitError('Allocated quota exceeded'), self.response]
        client = self.client()
        result = self.query(client)
        self.assertEqual(self.clock.sleeps, [60])
        self.assertEqual(self.completion.call_count, 2)
        self.assertEqual(result['content'], 'ok')
        self.assertEqual(client.stats()['calls'], 1)
        self.assertEqual(client.stats()['input_tokens'], 10)
        for call in self.completion.call_args_list:
            self.assertNotIn('rate_limit_retry_delay', call.kwargs)
            self.assertEqual(call.kwargs['num_retries'], 0)

    def test_repeated_rate_limits_keep_existing_two_retry_budget(self):
        self.completion.side_effect = [RateLimitError(), RateLimitError(), self.response]
        self.query()
        self.assertEqual(self.clock.sleeps, [60, 60])
        self.assertEqual(self.completion.call_count, 3)

    def test_exhausted_rate_limit_retries_still_pause_matching_peer(self):
        self.completion.side_effect = [RateLimitError(), self.response]
        with self.assertRaises(RateLimitError):
            self.query(self.client(num_retries=0))
        self.assertEqual(self.clock.sleeps, [])
        peer = self.client(temperature=0)  # Formatting shares the solver's model and credential.
        self.query(peer)
        self.assertEqual(self.clock.sleeps, [60])
        self.assertEqual(self.completion.call_count, 2)

    def test_new_rate_limit_extends_shared_wait_from_latest_error(self):
        self.completion.side_effect = [RateLimitError(), self.response]
        with self.assertRaises(RateLimitError):
            self.query(self.client(num_retries=0))
        self.clock.now += 10
        shared = model_module._RATE_LIMIT_COOLDOWNS
        key = next(iter(shared.until))
        shared.defer(key, 60)  # Another already-in-flight request has just returned 429.
        self.query()
        self.assertEqual(self.clock.sleeps, [60])
        self.assertEqual(self.clock.now, 170)

    def test_other_models_credentials_and_endpoints_do_not_share_wait(self):
        self.completion.side_effect = [RateLimitError(), self.response, self.response, self.response]
        with self.assertRaises(RateLimitError):
            self.query(self.client(num_retries=0))
        for options in ({'name': 'qwen3.8-max'}, {'api_key': 'another-test-key'},
                        {'api_base': 'https://other.invalid/v1'}):
            self.query(self.client(**options))
        self.assertEqual(self.clock.sleeps, [])

    def test_other_transient_errors_keep_exponential_wait(self):
        self.completion.side_effect = [ServiceUnavailableError(), ServiceUnavailableError(), self.response]
        self.query()
        self.assertEqual(self.clock.sleeps, [1, 2])
        self.assertEqual(self.completion.call_count, 3)

    def test_without_option_rate_limits_keep_old_waits(self):
        client = self.client()
        client.options.pop('rate_limit_retry_delay')
        self.completion.side_effect = [RateLimitError(), RateLimitError(), self.response]
        self.query(client)
        self.assertEqual(self.clock.sleeps, [1, 2])

    def test_non_retryable_error_is_raised_immediately(self):
        error = Exception('invalid credential')
        error.status_code = 401
        self.completion.side_effect = error
        with self.assertRaisesRegex(Exception, 'invalid credential'):
            self.query()
        self.assertEqual(self.completion.call_count, 1)
        self.assertEqual(self.clock.sleeps, [])

    def test_deadline_prevents_retry_or_peer_request_during_cooldown(self):
        self.completion.side_effect = RateLimitError()
        with self.assertRaises(RequestDeadlineExceeded):
            self.query(deadline=self.clock.now + 30)
        with self.assertRaises(RequestDeadlineExceeded):
            self.query(deadline=self.clock.now + 30)
        self.assertEqual(self.completion.call_count, 1)
        self.assertEqual(self.clock.sleeps, [])

    def test_cooldown_reduces_remaining_request_timeout(self):
        self.completion.side_effect = [RateLimitError(), self.response]
        self.query(deadline=self.clock.now + 100)
        self.assertEqual(self.clock.sleeps, [60])
        self.assertEqual([c.kwargs['timeout'] for c in self.completion.call_args_list], [100, 40])

    def test_wrapped_rate_limit_uses_same_cooldown(self):
        error = RuntimeError('wrapped provider error')
        error.__cause__ = RateLimitError()
        self.completion.side_effect = [error, self.response]
        self.query()
        self.assertEqual(self.clock.sleeps, [60])

    def test_invalid_delay_rejected_by_request_and_saved_config(self):
        import yaml
        config = load_config(ROOT / 'configs/agent/click15-flash-max.yaml')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.yaml'
            for value in (0, -1, True, '60', float('inf'), float('nan')):
                with self.subTest(delay=value):
                    with self.assertRaisesRegex(ValueError, 'rate_limit_retry_delay'):
                        request_options({'rate_limit_retry_delay': value})
                    config['model']['rate_limit_retry_delay'] = value
                    path.write_text(yaml.safe_dump(config), encoding='utf-8')
                    with self.assertRaisesRegex(ValueError, 'model.rate_limit_retry_delay'):
                        load_config(path)


if __name__ == '__main__':
    unittest.main()
