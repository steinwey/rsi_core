import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rsi.skills.budget import BudgetError, check_counter, render_context, select_context, token_counter
from rsi.skills.routing import route, settings
from rsi.skills import tokenizers as tk
from rsi.skills import tokenizer_setup
from rsi.core.common import read_json
from rsi.core.config import load_config
from rsi.agent.runner import run_experiment
from rsi.experiment.checkpoints import resume_request
from routing_fixtures import CharacterCounter, files
import test_skill_routing as p2
from test_core_baseline import Backend


def model(name='gpt-6-luna'):
    return {'name': name, 'provider': 'sub2api' if name == 'gpt-6-luna' else 'qwen-token-plan',
            'api_base': 'https://example.invalid/v1'}


def options(cfg):
    return {'method': tk.METHOD, 'encoding': tk.ENCODING, 'tokenizer_version': tk.VERSION,
            'model': cfg['name'], 'provider': cfg['provider'], 'api_base': cfg['api_base'], 'safety_margin': .25}


def estimated(cfg):
    return {'enabled': True, 'budget_mode': 'estimated', 'estimated_counter': options(cfg)}


class TokenizerTests(unittest.TestCase):
    def test_all_three_models_select_explicit_estimate(self):
        for name in ('qwen3.8-max', 'qwen3.8-flash', 'gpt-6-luna'):
            cfg = model(name)
            with self.subTest(name=name):
                counter = token_counter(cfg, mode='estimated', estimated=options(cfg))
                self.assertEqual(counter.info.model, name)
                self.assertEqual(counter.info.accuracy, 'estimated')
                self.assertGreater(counter.count('pytest fixture 中文'), 0)
                manifest = json.loads(counter.info.identity)
                self.assertEqual(manifest['vocabulary_sha256'], tk.VOCAB_SHA256)
                self.assertEqual(manifest['version'], '0.14.0')

    def test_all_three_strict_still_fail_without_loading_proxy(self):
        with patch.object(tk, 'load_encoding', side_effect=AssertionError('strict fallback')):
            for name in ('qwen3.8-max', 'qwen3.8-flash', 'gpt-6-luna'):
                with self.subTest(name=name), self.assertRaises(BudgetError): token_counter(model(name))

    def test_unknown_alias_rejected_in_both_modes(self):
        cfg = model('openai/gpt-6-luna')
        with self.assertRaises(BudgetError): token_counter(cfg)
        with self.assertRaises(ValueError): token_counter(cfg, mode='estimated', estimated=options(cfg))

    def test_binding_and_version_mismatch_rejected(self):
        cfg = model()
        for key, value in [('model', 'qwen3.8-max'), ('provider', 'other'), ('api_base', 'https://other.invalid'),
                           ('tokenizer_version', '0.15.0'), ('encoding', 'cl100k_base'), ('method', 'chars_div_4')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                tk.validate_estimated(cfg, options(cfg) | {key: value})

    def test_installed_version_mismatch_fails(self):
        with patch('importlib.metadata.version', return_value='0.13.0'), self.assertRaises(BudgetError):
            tk.load_encoding()

    def test_missing_vocab_does_not_download(self):
        with tempfile.TemporaryDirectory() as tmp, patch('urllib.request.urlopen', side_effect=AssertionError('network')):
            with self.assertRaises(BudgetError): tk.load_encoding(Path(tmp) / 'absent')

    def test_corrupt_vocab_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'vocab'; path.write_bytes(b'corrupt')
            with self.assertRaises(BudgetError): tk.load_encoding(path)

    def test_actual_bpe_deterministic_and_special_markers_literal(self):
        counter = token_counter(model(), mode='estimated', estimated=options(model()))
        samples = ['Hello world!', '中文测试', 'get_absolute_url AttributeError', '<|endoftext|>', '😀\n```python\nx=1\n```']
        for text in samples:
            with self.subTest(text=text):
                tokens = counter._encoding.encode_ordinary(text)
                self.assertEqual(counter._encoding.decode(tokens), text)
                self.assertEqual(counter.count(text), len(tokens))
                self.assertEqual(counter.count(text), counter.count(text))
        self.assertEqual(counter.count('Hello world!'), 3)
        self.assertGreater(counter.count('<|endoftext|>'), 1)

    def test_real_encoding_matches_pinned_official_constructor(self):
        import tiktoken
        import tiktoken_ext.openai_public as public
        actual = tk.load_encoding()
        # Only replace the official loader with the already SHA-verified local ranks.
        # This checks our pattern/specials against upstream without any network request.
        with patch.object(public, 'load_tiktoken_bpe', return_value=actual._mergeable_ranks):
            official = tiktoken.Encoding(**public.o200k_base())
        for text in ['Hello world!', '中文错误', 'get_absolute_url HTTPServer', '123456789',
                     '   \n \t', "We're checking it's complete", '<|endoftext|>', '😀🌏']:
            self.assertEqual(actual.encode_ordinary(text), official.encode_ordinary(text))

    def test_counter_mode_isolation(self):
        counter = token_counter(model(), mode='estimated', estimated=options(model()))
        with self.assertRaises(BudgetError): check_counter(counter, 'gpt-6-luna')
        self.assertEqual(check_counter(counter, 'gpt-6-luna', mode='estimated').accuracy, 'estimated')
        with self.assertRaises(BudgetError): check_counter(CharacterCounter('gpt-6-luna'), 'gpt-6-luna', mode='estimated')
        with self.assertRaises(BudgetError): token_counter(model(), estimated=options(model()))

    def test_margin_and_exact_decimal_boundary(self):
        self.assertEqual(tk.effective_limit(4000, .25), 3200)
        self.assertEqual(tk.effective_limit(4000, 0), 4000)
        self.assertEqual(tk.effective_limit(1100, .1), 1000)
        for value in (-.1, 11, True, '0.25', float('nan'), float('inf')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                tk.validate_estimated(model(), options(model()) | {'safety_margin': value})

    def test_margin_selection_boundary(self):
        source = files()
        source['SKILL.md'] += 'x' * (3200 - len(render_context(source)))
        counter = CharacterCounter()
        limit = tk.effective_limit(4000, .25)
        result = select_context(source, [], counter, budget_limit=limit, top_k=2)
        self.assertEqual(result.context.initial_skill_tokens, 3200)
        source['SKILL.md'] += 'x'
        with self.assertRaises(BudgetError): select_context(source, [], counter, budget_limit=limit, top_k=2)

    def test_strict_defaults_and_logs_config_unchanged(self):
        self.assertNotIn('estimated_counter', settings({}))
        with self.assertRaises(ValueError): settings({'skill_routing': {'budget_mode': 'estimated'}})
        with self.assertRaises(ValueError): settings({'skill_routing': {'estimated_counter': options(model())}})
        cfg = {'model': model(), 'skill_routing': estimated(model())}
        self.assertEqual(settings(cfg)['estimated_counter'], options(model()))

    def test_explicit_setup_bad_hash_never_installs(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'source'; source.write_bytes(b'bad')
            dest = Path(tmp) / 'cache'
            with patch.object(tokenizer_setup, 'CACHE_PATH', dest), self.assertRaises(ValueError): tokenizer_setup.install(source)
            self.assertFalse(dest.exists())

    def test_explicit_setup_offline_copy_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / 'cache'
            with patch.object(tokenizer_setup, 'CACHE_PATH', dest), patch('urllib.request.urlopen', side_effect=AssertionError('network')):
                self.assertEqual(tokenizer_setup.install(tk.CACHE_PATH), dest)
                self.assertEqual(tokenizer_setup.install(), dest)

    def test_corrupt_existing_setup_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / 'cache'; dest.write_bytes(b'bad')
            with patch.object(tokenizer_setup, 'CACHE_PATH', dest), self.assertRaises(ValueError): tokenizer_setup.install(tk.CACHE_PATH)
            self.assertEqual(dest.read_bytes(), b'bad')


class EstimatedIntegrationTests(unittest.TestCase):
    def setUp(self):
        p2.RoutingIntegrationTests.setUp(self)
        self.config['model'].update(model())
        self.config['skill_routing'] = settings({'model': self.config['model'], 'skill_routing': estimated(self.config['model'])})

    def run_tasks(self, resume=False):
        def fake_models(cfg):
            return self.models(cfg | {'name': 'solver'} if cfg['name'] == 'gpt-6-luna' else cfg)
        return run_experiment(self.tasks, self.config, self.output, resume=resume, backend_factory=Backend, model_factory=fake_models)

    def test_real_local_counter_once_shared_five_routes_and_resume(self):
        with patch('rsi.skills.tokenizers.load_encoding', wraps=tk.load_encoding) as loader, \
                patch('urllib.request.urlopen', side_effect=AssertionError('runtime network')):
            reports = self.run_tasks()
            self.assertEqual(loader.call_count, 1)
            self.run_tasks(resume=True)
            self.assertEqual(loader.call_count, 1)
        record = read_json(self.output / 'task/skill-route.json')
        self.assertEqual(record['counter']['accuracy'], 'estimated')
        self.assertEqual(record['estimated_budget']['effective_limit'], 3200)
        self.assertFalse(record['estimated_budget']['strict_guarantee'])
        text = (self.output / 'task/skill-context.txt').read_text()
        self.assertEqual(record['initial_skill_tokens'], len(tk.load_encoding().encode_ordinary(text)))
        self.assertEqual(record['selected_references'], ['references/pytest.md'])
        for i in range(5):
            trajectory = read_json(self.output / f'task/rollout-{i}/trajectory.json')
            self.assertEqual(trajectory['route_id'], record['route_id'])
            self.assertEqual(trajectory['context_hash'], record['context_hash'])
            system = next(m['content'] for m in trajectory['messages'] if m['role'] == 'system')
            self.assertTrue(system.endswith(text))
        self.assertEqual(reports[0]['graded_rollouts'], list(range(5)))
        resume_request(self.output)

    def test_changing_margin_changes_route_id_not_relevance(self):
        first = route(self.tasks[0], self.seed, self.config)
        self.config['skill_routing']['estimated_counter']['safety_margin'] = .5
        second = route(self.tasks[0], self.seed, self.config)
        self.assertNotEqual(first.context.route_id, second.context.route_id)
        self.assertEqual(first.record()['candidate_scores'], second.record()['candidate_scores'])
        self.assertEqual(first.context.text, second.context.text)

    def test_changed_binding_rejected_before_workers(self):
        self.config['model']['api_base'] = 'https://changed.invalid'
        with self.assertRaises(ValueError): self.run_tasks()
        self.assertEqual(Backend.instances, [])

    def test_changed_margin_on_resume_rejected(self):
        self.run_tasks(); self.config['skill_routing']['estimated_counter']['safety_margin'] = .5
        with self.assertRaisesRegex(ValueError, 'without overrides'): self.run_tasks(resume=True)

    def test_switching_to_strict_does_not_auto_fallback(self):
        self.config['skill_routing'] = {'enabled': True}
        with self.assertRaises(BudgetError): self.run_tasks()
        record = read_json(self.output / 'task/skill-route.json')
        self.assertEqual(record['reason'], 'counter_unavailable')
        self.assertEqual(Backend.instances, [])


class CompatibilityTests(unittest.TestCase):
    def test_p2_strict_route_id_matches_archived_example(self):
        case = p2.RoutingIntegrationTests('test_once_before_pool_five_identical_actual_contexts')
        case.setUp()
        try:
            case.config['skill_routing']['max_initial_skill_tokens'] = 4000
            result = route(case.tasks[0], case.seed, case.config, counter_factory=case.counter_factory)
            self.assertEqual(result.context.route_id, 'e951d4dd246e9af584460c4d4c0f87fcfb4f67d953dcdc879bc9cea9b38c5e9c')
            self.assertEqual(result.context.initial_skill_tokens, 1883)
            self.assertNotIn('estimated_budget', result.record())
        finally:
            case.doCleanups()

    def test_launch_configs_validate_and_preserve_original_roles(self):
        from rsi import ROOT
        for original, new in [('qwen.yaml', 'routing-estimated-qwen-max.yaml'),
                              ('click15-flash-max.yaml', 'routing-estimated-qwen-flash.yaml'),
                              ('gpt6-luna.yaml', 'routing-estimated-gpt6-luna.yaml')]:
            before = load_config(ROOT / 'configs/agent' / original)
            after = load_config(ROOT / 'configs/agent' / new)
            self.assertEqual(after['skill_routing']['budget_mode'], 'estimated')
            for key in before.keys() - {'skill_directory', 'skill_routing'}:
                self.assertEqual(before[key], after[key])
            self.assertEqual(token_counter(after['model'], mode='estimated',
                estimated=after['skill_routing']['estimated_counter']).info.accuracy, 'estimated')
