import unittest
from dataclasses import replace
from unittest.mock import Mock

from rsi.skills.budget import (BudgetError, CounterInfo, check_counter, count_text,
                               render_context, select_context, token_counter, UNSUPPORTED_MODELS)
from rsi.skills.index import sha
from routing_fixtures import CharacterCounter, files


class BudgetTests(unittest.TestCase):
    def test_complete_text_includes_all_wrappers(self):
        source = files({'references/x.md': '# X\nFULL BODY\n'})
        counter = CharacterCounter()
        result = select_context(source, ['references/x.md'], counter, budget_limit=4000, top_k=2)
        text = result.context.text
        for part in ('## Skill toolkit', 'python /opt/rsi-skills/scripts/check.py',
                     '/opt/rsi-skills/references/', '/opt/rsi-skills/routing/index.yaml',
                     'Automatically loaded references:', 'references/x.md (complete file)',
                     source['SKILL.md'], source['references/x.md'], '---'):
            self.assertIn(part, text)
        self.assertEqual(counter.calls[-1], text)
        self.assertEqual(result.context.initial_skill_tokens, len(text))
        self.assertEqual(result.context.context_hash, sha(text))

    def test_exact_4000_core_allowed(self):
        source = files()
        source['SKILL.md'] += 'x' * (4000 - len(render_context(source)))
        result = select_context(source, [], CharacterCounter(), budget_limit=4000, top_k=2)
        self.assertEqual(result.context.initial_skill_tokens, 4000)

    def test_core_4001_fails_without_truncation(self):
        source = files()
        source['SKILL.md'] += 'x' * (4001 - len(render_context(source)))
        with self.assertRaises(BudgetError) as caught:
            select_context(source, [], CharacterCounter(), budget_limit=4000, top_k=2)
        self.assertEqual(caught.exception.code, 'core_over_budget')
        self.assertEqual(len(render_context(source)), 4001)

    def test_exact_4000_with_reference_allowed(self):
        path = 'references/a.md'
        source = files({path: '# Reference\n'})
        source[path] += 'z' * (4000 - len(render_context(source, [path])))
        result = select_context(source, [path], CharacterCounter(), budget_limit=4000, top_k=2)
        self.assertEqual(result.context.initial_skill_tokens, 4000)
        self.assertEqual(result.context.selected_references, (path,))
        self.assertTrue(result.context.text.endswith(source[path]))

    def test_reference_4001_skips_whole_file(self):
        path = 'references/a.md'; source = files({path: 'Unique body'})
        source[path] += 'z' * (4001 - len(render_context(source, [path])))
        result = select_context(source, [path], CharacterCounter(), budget_limit=4000, top_k=2)
        self.assertEqual(result.context.selected_references, ())
        self.assertEqual(result.excluded, ((path, 'reference_over_budget', 4001),))
        self.assertNotIn('Unique body', result.context.text)

    def test_large_candidate_skipped_then_smaller_accepted(self):
        source = files({'references/large.md': 'large' * 1000, 'references/small.md': 'Small complete\n'})
        result = select_context(source, ['references/large.md', 'references/small.md'],
                                CharacterCounter(), budget_limit=4000, top_k=1)
        self.assertEqual(result.context.selected_references, ('references/small.md',))
        self.assertTrue(result.context.text.endswith(source['references/small.md']))

    def test_top_k_counts_accepted_files_only(self):
        source = files({f'references/{n}.md': 'content' for n in range(4)})
        result = select_context(source, [f'references/{n}.md' for n in range(4)],
                                CharacterCounter(), budget_limit=4000, top_k=2)
        self.assertEqual(len(result.context.selected_references), 2)
        self.assertEqual([reason for _, reason, _ in result.excluded], ['top_k_limit'] * 2)

    def test_no_candidates_keeps_complete_core(self):
        source = files(root='# Core\n你好 😀 {{ braces }}\n')
        result = select_context(source, [], CharacterCounter(), budget_limit=4000, top_k=2)
        self.assertEqual(result.context.text, render_context(source))
        self.assertIn(source['SKILL.md'], result.context.text)

    def test_count_is_not_sum_of_individual_fragments(self):
        counter = CharacterCounter()
        # Synthetic non-additive vocabulary: every whole message has an extra token.
        counter.count = lambda text: len(text) + 1
        result = select_context(files({'references/a.md': 'body'}), ['references/a.md'],
                                counter, budget_limit=4000, top_k=2)
        self.assertEqual(result.context.initial_skill_tokens, len(result.context.text) + 1)

    def test_final_recount_detects_inconsistent_counter(self):
        counter = CharacterCounter()
        counter.count = Mock(side_effect=[100, 4001])
        with self.assertRaises(BudgetError) as caught:
            select_context(files(), [], counter, budget_limit=4000, top_k=2)
        self.assertEqual(caught.exception.code, 'final_over_budget')

    def test_all_target_models_fail_closed(self):
        self.assertEqual(set(UNSUPPORTED_MODELS), {'gpt-6-luna', 'qwen3.8-max', 'qwen3.8-flash'})
        for name in UNSUPPORTED_MODELS:
            with self.subTest(name=name), self.assertRaises(BudgetError) as caught:
                token_counter({'name': name})
            self.assertEqual(caught.exception.code, 'counter_unavailable')

    def test_unknown_model_never_guessed(self):
        with self.assertRaises(BudgetError):
            token_counter({'name': 'openai/gpt-6-luna-custom'})

    def test_counter_model_identity_checked(self):
        counter = CharacterCounter('gpt-6-luna')
        with self.assertRaises(BudgetError):
            check_counter(counter, 'qwen3.8-max')
        self.assertEqual(check_counter(counter, 'gpt-6-luna').accuracy, 'test_only')

    def test_estimated_counter_rejected(self):
        counter = CharacterCounter()
        counter.info = replace(counter.info, accuracy='chars_div_4')
        with self.assertRaises(BudgetError):
            check_counter(counter, 'mock-solver')

    def test_bad_counter_values_rejected(self):
        counter = CharacterCounter()
        for value in (True, -1, 1.1, None, 0):
            counter.count = lambda text: value
            with self.subTest(value=value), self.assertRaises(BudgetError):
                count_text(counter, 'nonempty')

    def test_counter_exception_is_not_zero_match(self):
        counter = CharacterCounter(); counter.count = Mock(side_effect=OSError('broken tokenizer'))
        with self.assertRaises(BudgetError) as caught:
            count_text(counter, 'body')
        self.assertEqual(caught.exception.code, 'counter_error')

    def test_context_is_frozen(self):
        from dataclasses import FrozenInstanceError
        result = select_context(files(), [], CharacterCounter(), budget_limit=4000, top_k=2)
        with self.assertRaises(FrozenInstanceError):
            result.context.text = 'mutated'
