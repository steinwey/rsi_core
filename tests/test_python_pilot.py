import copy
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.run_python_pilot import check_manifests, evaluation_spec


class PythonPilotTests(unittest.TestCase):
    def test_prepared_split_matches_cached_python_grading_metadata(self):
        groups, split = check_manifests()
        for group in groups.values():
            self.assertEqual(len(group), 10)
            self.assertEqual(sum(t['instance']['repo'] == 'pallets/click' for t in group), 5)
            self.assertEqual(sum(t['instance']['repo'] == 'sqlfluff/sqlfluff' for t in group), 5)
        self.assertNotIn('evo-f881cb75-000', {t['id'] for g in groups.values() for t in g})
        for repo in ('pallets/click', 'sqlfluff/sqlfluff'):
            rows = [t for t in split['tasks'] if t['repo'] == repo]
            self.assertLess(max(t['source_chain_index'] for t in rows if t['split'] == 'train'),
                            min(t['source_chain_index'] for t in rows if t['split'] == 'eval'))

    def spec(self, *, status='completed', mode='evolve', mismatch=False):
        expected = [{'id': f'task-{i}'} for i in range(10)]
        actual = copy.deepcopy(expected)
        if mismatch:
            actual.reverse()
        info = {'status': status, 'mode': mode, 'completed_tasks': 10, 'scored_tasks': 0, 'supervised_tasks': 10,
                'current_skill_version': '0123456789abcdef'}
        def read(path):
            return {'run-info.json': info, 'task-manifest.json': actual,
                    'current.json': {'version': '0123456789abcdef'}}[path.name]
        with patch('scripts.run_python_pilot.read_json', side_effect=read), \
             patch('scripts.run_python_pilot.load_config', return_value={
                 'evolve': True, 'swe_dataset': 'cached-data.json', 'swe_split': 'test'}), \
             patch('scripts.run_python_pilot.load_initial_skill') as validate_skill:
            spec = evaluation_spec(Path('runs/fake-training'), {'train': expected},
                                   {'evaluation_dataset': 'cached-data.json'})
            validate_skill.assert_called_once()
            return spec

    def test_evaluation_pins_final_version_and_uses_saved_training_config(self):
        spec = self.spec()
        self.assertTrue(spec['fixed_skill'])
        self.assertTrue(spec['initial_skill'].replace('\\', '/').endswith('/skills/versions/0123456789abcdef'))
        self.assertTrue(spec['config'].replace('\\', '/').endswith('/fake-training/config.json'))
        self.assertEqual(spec['rollouts'], 5)

    def test_incomplete_wrong_mode_and_reordered_training_are_rejected(self):
        for options in ({'status': 'running'}, {'mode': 'fixed'}, {'mismatch': True}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.spec(**options)
