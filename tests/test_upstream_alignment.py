import unittest
from unittest.mock import patch

from rsi.skills.trace2skill.upstream import load
from test_core_baseline import Models, test_config
from test_trace2skill import Trace2SkillTests


class UpstreamParsingTests(unittest.TestCase):
    def test_report_uses_official_parser_without_exact_quote_gate(self):
        load()
        from analysis.parse_error_analysis_outputs import parse_items
        text = '''# Failure Memory Item 1

## Title
Preserve missing-value distinctions

## Description
Check callers before hiding sentinel values.

## Content
The diagnostic excerpt uses escaped quotes: assert 'x' in \\"output\\".
'''
        items = parse_items(text)
        self.assertEqual(len(items), 1)
        self.assertIn('escaped quotes', items[0]['content'])


class UpstreamFallbackTests(unittest.TestCase):
    setUp = Trace2SkillTests.setUp
    run_task = Trace2SkillTests.run_task
    def test_invalid_map_reply_is_handled_by_official_editor(self):
        class InvalidEditor(Models):
            def __call__(owner, config):
                client = super().__call__(config)
                query = client.query
                def respond(messages, **kwargs):
                    if client.name == 'judge':
                        client.n_calls += 1
                        return {'content': 'No parseable patch here'}
                    return query(messages, **kwargs)
                client.query = respond
                return client
        self.run_task(InvalidEditor(), test_config())
        from rsi.core.common import read_json
        update = read_json(self.output / 'example/update.json')
        self.assertEqual(update['decision'], 'no_changes')
