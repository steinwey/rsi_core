import copy
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from rsi import ROOT
from rsi.agent.runner import run_experiment
from rsi.core.common import read_json, write_json
from rsi.core.config import load_config
from rsi.experiment import checkpoints
from rsi.skills.trace2skill import evolve_skill
from scripts.monitor_experiment import activity_paths, process_alive
from test_core_baseline import Backend, Models


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config(ROOT / 'configs/agent/qwen.yaml')
        for role, name in (('model', 'solver'), ('judge_model', 'judge'), ('skill_editor_model', 'judge'),
                           ('skill_error_model', 'error_analyst'), ('skill_success_model', 'success_analyst')):
            self.config[role]['name'] = name
        Backend.barrier, Backend.instances = threading.Barrier(5), []
        self.tasks = [{'id': f'task-{i}', 'benchmark': 'swe', 'prompt': 'Fix visible issue'} for i in range(2)]
        self.models = Models()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / 'run'
        gate = patch('rsi.agent.runner.functional_gate', return_value={'passed': True})
        gate.start()
        self.addCleanup(gate.stop)

    def run_tasks(self, resume=False, tasks=None):
        return run_experiment(tasks or self.tasks, self.config, self.output, resume=resume,
                              backend_factory=Backend, model_factory=self.models)

    def test_resume_skips_committed_task_archives_partial_and_inherits_skill(self):
        def stop_second(task, *args, **kwargs):
            if task['id'] == 'task-1':
                raise KeyboardInterrupt('injected interruption')
            return evolve_skill(task, *args, **kwargs)
        with patch('rsi.agent.runner.evolve_skill', side_effect=stop_second), self.assertRaises(KeyboardInterrupt):
            self.run_tasks()
        state, saved, groups = checkpoints.resume_request(self.output)
        self.assertEqual(len(state['completed']), 1)
        first = self.output / 'task-0/rollout-0/trajectory.json'
        before = first.read_bytes()
        version = state['current_skill_version']
        self.models.requests.clear()
        reports = self.run_tasks(resume=True)
        self.assertEqual(first.read_bytes(), before)
        self.assertEqual(len(list((self.output / 'attempts').iterdir())), 1)
        self.assertEqual(reports[1]['skill_version'], version)
        self.assertEqual(len([r for n, r in self.models.requests if n == 'solver']), 5)
        self.assertEqual(len((self.output / 'skills/history.jsonl').read_text().splitlines()), 2)
        self.assertEqual(read_json(self.output / 'metrics.json')['tokens'], sum(r['tokens'] for r in reports))
        self.assertTrue(all(b.closed for b in Backend.instances))

    def test_crash_before_commit_leaves_pointer_unchanged(self):
        real_write = checkpoints.write_json
        def fail_commit(path, value):
            if path.name == 'checkpoint.json' and value['completed']:
                raise KeyboardInterrupt('before commit')
            return real_write(path, value)
        with patch('rsi.experiment.checkpoints.write_json', side_effect=fail_commit), self.assertRaises(KeyboardInterrupt):
            self.run_tasks(tasks=self.tasks[:1])
        state, _, _ = checkpoints.resume_request(self.output)
        self.assertEqual(state['completed'], [])
        self.assertEqual(read_json(self.output / 'skills/current.json')['version'], state['initial_skill_version'])
        self.run_tasks(resume=True, tasks=self.tasks[:1])
        self.assertEqual(len((self.output / 'skills/history.jsonl').read_text().splitlines()), 1)

    def test_crash_after_commit_replays_without_model_calls_or_duplicate_updates(self):
        with patch('rsi.experiment.checkpoints.publish', side_effect=KeyboardInterrupt('after commit')), self.assertRaises(KeyboardInterrupt):
            self.run_tasks(tasks=self.tasks[:1])
        state, _, _ = checkpoints.resume_request(self.output)
        self.assertEqual(len(state['completed']), 1)
        self.models.requests.clear()
        self.run_tasks(resume=True, tasks=self.tasks[:1])
        self.run_tasks(resume=True, tasks=self.tasks[:1])
        self.assertEqual(self.models.requests, [])
        self.assertEqual(len((self.output / 'skills/history.jsonl').read_text().splitlines()), 1)
        self.assertEqual(read_json(self.output / 'skills/current.json')['version'], state['current_skill_version'])
        self.assertEqual(read_json(self.output / 'run-info.json')['status'], 'completed')

    def test_unscored_task_is_not_committed_and_is_retried(self):
        with patch.object(Backend, 'score', return_value={'success': None, 'error': 'temporary harness failure'}), self.assertRaisesRegex(RuntimeError, 'Scoring'):
            self.run_tasks(tasks=self.tasks[:1])
        self.assertEqual(read_json(self.output / 'checkpoint.json')['completed'], [])
        self.assertFalse((self.output / 'task-0/proposal.json').exists())
        self.run_tasks(resume=True, tasks=self.tasks[:1])
        self.assertEqual(len(read_json(self.output / 'checkpoint.json')['completed']), 1)

    def test_fixed_skill_resume_and_config_tampering(self):
        self.config['evolve'] = False
        self.run_tasks(tasks=self.tasks[:1])
        state, _, _ = checkpoints.resume_request(self.output)
        self.assertEqual(state['initial_skill_version'], state['current_skill_version'])
        self.run_tasks(resume=True, tasks=self.tasks[:1])
        altered = copy.deepcopy(self.config)
        altered['step_limit'] += 1
        write_json(self.output / 'config.json', altered)
        with self.assertRaisesRegex(ValueError, 'mismatch'):
            checkpoints.resume_request(self.output)

    def test_timing_includes_trace2skill(self):
        def slow_update(*args, **kwargs):
            time.sleep(0.02)
            return evolve_skill(*args, **kwargs)
        with patch('rsi.agent.runner.evolve_skill', side_effect=slow_update):
            report = self.run_tasks(tasks=self.tasks[:1])[0]
        self.assertGreaterEqual(report['skill_update_seconds'], 0.02)
        self.assertGreaterEqual(report['seconds'], report['group_seconds'] + report['skill_update_seconds'])
        self.assertEqual(read_json(self.output / 'metrics.json')['seconds'], report['seconds'])

    def test_activity_includes_analysis_and_lock_rejects_concurrent_writer(self):
        self.output.mkdir()
        path = self.output / 'task/trace2skill/rollout-0/calls/attempt-1.request.json'
        write_json(path, {})
        self.assertIn(path, activity_paths(self.output / 'task'))
        with checkpoints.run_lock(self.output), self.assertRaises(RuntimeError):
            with checkpoints.run_lock(self.output):
                pass
        with checkpoints.run_lock(self.output):
            pass
        import os
        self.assertTrue(process_alive(os.getpid()))
        self.assertFalse(process_alive(None))

    def test_legacy_run_has_clear_error(self):
        self.output.mkdir()
        with self.assertRaisesRegex(ValueError, 'no task checkpoint'):
            checkpoints.resume_request(self.output)

    def test_lock_is_released_after_abrupt_process_exit(self):
        import subprocess
        import sys
        self.output.mkdir()
        code = ('import os,sys; from rsi.experiment.checkpoints import run_lock; '
                'lock=run_lock(sys.argv[1]); lock.__enter__(); os._exit(23)')
        process = subprocess.run([sys.executable, '-c', code, str(self.output)], cwd=ROOT,
                                 capture_output=True, timeout=20)
        self.assertEqual(process.returncode, 23, process.stderr)
        with checkpoints.run_lock(self.output):
            pass
