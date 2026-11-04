import copy
import tempfile
import unittest
from pathlib import Path

import yaml

from rsi import ROOT
from rsi.core.config import load_config, model_configs, select_model


class LunaConfigTests(unittest.TestCase):
    def test_luna_profile_and_same_model_selection(self):
        config = load_config(ROOT / 'configs/agent/gpt6-luna.yaml')
        self.assertIs(select_model(config, 'gpt-6-luna'), config)
        with self.assertRaisesRegex(ValueError, 'switch providers'):
            select_model(config, 'qwen3.8-max')
        baseline = load_config(ROOT / 'configs/agent/qwen.yaml')
        for role, block in model_configs(config).items():
            self.assertNotIn('api_key', block)
            if role == 'model':
                self.assertEqual(block['name'], 'gpt-6-luna')
                self.assertEqual(block['api_key_env'], 'SUB2API_API_KEY')
                self.assertNotIn('extra_body', block)
            else:
                self.assertEqual(block, baseline[role])

    def test_luna_requires_qwen_for_other_roles(self):
        source = load_config(ROOT / 'configs/agent/gpt6-luna.yaml')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.yaml'
            for role in ('judge_model', 'skill_editor_model'):
                config = copy.deepcopy(source)
                config[role]['name'] = 'gpt-6-luna'
                path.write_text(yaml.safe_dump(config), encoding='utf-8')
                with self.assertRaisesRegex(ValueError, role):
                    load_config(path)


if __name__ == '__main__':
    unittest.main()
