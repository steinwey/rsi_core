import unittest
from unittest.mock import patch

from rsi import ROOT
from scripts.run_click_chain import check_manifests, main


class ClickChainTests(unittest.TestCase):
    def test_exact_chronological_split(self):
        groups, split = check_manifests()
        self.assertEqual([t['id'] for t in groups['train']], [f'evo-f881cb75-{i:03}' for i in range(10)])
        self.assertEqual([t['id'] for t in groups['eval']], [f'evo-f881cb75-{i:03}' for i in range(10, 15)])
        self.assertEqual(split['repo'], 'pallets/click')

    def test_failed_training_never_starts_evaluation(self):
        with patch('sys.argv', ['run_click_chain', 'run']), \
             patch('scripts.run_click_chain.check_manifests', return_value=({'train': [], 'eval': []}, {})), \
             patch('scripts.run_click_chain.require_local_images'), \
             patch('scripts.run_click_chain.write_json'), \
             patch('scripts.run_click_chain.read_json', return_value={'output': str(ROOT / 'runs/fake')}), \
             patch('scripts.run_click_chain.monitor', return_value=1) as monitor, \
             patch('scripts.run_click_chain.evaluation_spec') as evaluate:
            with self.assertRaisesRegex(RuntimeError, 'Training monitor stopped'):
                main()
            self.assertEqual(monitor.call_count, 1)
            evaluate.assert_not_called()
