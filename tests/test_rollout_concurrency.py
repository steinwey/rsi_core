import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from rsi.agent.runner import run_group
from rsi.agent.rollouts import set_execution_mode, validate_execution_mode
from rsi.core.common import write_json
from rsi.core.config import load_config
from rsi.skills.store import SkillStore, load_initial_skill
from rsi import ROOT


class RolloutConcurrencyTests(unittest.TestCase):
    def test_five_workers_produce_five_saved_routes_before_grading(self):
        config = load_config(ROOT / 'configs/agent/click15-flash-max.yaml')
        self.assertEqual(config['rollouts'], 5)
        self.assertEqual(config['rollout_workers'], 5)
        lock, initial = threading.Lock(), threading.Barrier(5)
        four_saved, release_last = threading.Event(), threading.Event()
        active = peak = 0
        saved, scored = [], []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            files, _ = load_initial_skill()
            skill = SkillStore(root / 'skills', files).current()
            task_folder = root / 'task'

            class Handle:
                def close(self):
                    pass

                def score(self, artifact):
                    # No grading is allowed before all five outputs are saved.
                    self_outer.assertEqual(len(list(task_folder.glob('rollout-*/trajectory.json'))), 5)
                    scored.append(artifact)
                    return {'success': True}

            self_outer = self

            def execute(task, index, folder, skill, *args):
                nonlocal active, peak
                with lock:
                    active += 1
                    peak = max(peak, active)
                try:
                    if index < 5:
                        initial.wait(timeout=5)
                    if index == 4:
                        if not release_last.wait(timeout=10):
                            raise RuntimeError('Test did not release final route')
                    result = {'rollout': index, 'artifact': str(index), 'error': None,
                              'skill_injected': True, 'usage': {'input_tokens': 1, 'output_tokens': 1}}
                    write_json(folder / 'trajectory.json', result)
                    with lock:
                        saved.append(index)
                        if len(saved) == 4:
                            four_saved.set()
                    return result, Handle()
                finally:
                    with lock:
                        active -= 1

            with patch('rsi.agent.runner.execute_one', side_effect=execute), \
                 patch('rsi.agent.runner.full_evidence', return_value=({}, 1)), \
                 ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(run_group, {'id': 'task', 'benchmark': 'swe'}, task_folder, skill, config)
                try:
                    self.assertTrue(four_saved.wait(timeout=5))
                    self.assertEqual(len(list(task_folder.glob('rollout-*/trajectory.json'))), 4)
                    self.assertEqual(scored, [])
                finally:
                    release_last.set()
                report, results, _ = future.result(timeout=5)
            self.assertEqual(peak, 5)
            self.assertEqual([r['rollout'] for r in results], list(range(5)))
            self.assertEqual(len(scored), 5)
            self.assertEqual(report['graded_rollouts'], list(range(5)))
            self.assertEqual(report['rollout_workers'], 5)

    def test_serial_generation_saves_each_route_before_starting_the_next(self):
        from test_core_baseline import Backend, Models, test_config
        config = test_config()
        config['rollout_workers'] = 1
        owner = self

        class SerialBackend(Backend):
            instances = []
            barrier = threading.Barrier(1)

            def __init__(self, task, folder, config):
                index = int(folder.name.split('-')[-1])
                for previous in range(index):
                    owner.assertTrue((folder.parent / f'rollout-{previous}/trajectory.json').exists())
                super().__init__(task, folder, config)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            files, _ = load_initial_skill()
            skill = SkillStore(root / 'skills', files).current()
            report, results, _ = run_group({'id': 'serial', 'benchmark': 'swe', 'prompt': 'Fix visible issue'},
                root / 'task', skill, config, backend_factory=SerialBackend, model_factory=Models())
            self.assertEqual(len(SerialBackend.instances), 5)
            self.assertEqual(report['graded_rollouts'], list(range(5)))
            self.assertEqual(report['rollout_workers'], 1)
            self.assertEqual({r['skill_version'] for r in results}, {skill.name})

    def test_single_mode_caps_workers_and_allows_single_route_training(self):
        config = load_config(ROOT / 'configs/agent/click15-flash-max.yaml')
        config['evolve'] = False
        set_execution_mode(config, 'single')
        self.assertEqual((config['rollouts'], config['rollout_workers']), (1, 1))
        self.assertEqual(validate_execution_mode(config), 'single')
        config['evolve'] = True
        self.assertEqual(validate_execution_mode(config), 'single')
