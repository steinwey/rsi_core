import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from rsi.core.common import read_json, write_json
from scripts import replay_frameworks as replay


class ReplayMonitorTests(unittest.TestCase):
    def run_monitor(self, states, *, cleanup=None):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        args = SimpleNamespace(output=Path(temp.name) / 'replay', source=Path(temp.name) / 'source',
                               config=Path(temp.name) / 'config.json', task_id='example', timeout=60)
        children = []

        def start(command, **kwargs):
            method = command[command.index('--worker') + 1]
            status = states[method]
            child = SimpleNamespace(pid=100 + len(children), code=None if status == 'live' else (0 if status == 'completed' else 1))
            child.poll = lambda: child.code
            def wait(**kwargs):
                child.code = 1
                return child.code
            child.wait = wait
            children.append(child)
            if status != 'live':
                write_json(args.output / method / 'result.json', {'status': status})
            return child

        with patch.object(replay, 'FRAMEWORKS', tuple(states)), \
                patch.object(replay, 'load_env'), patch.object(replay, 'load_config', return_value={}), \
                patch.object(replay, 'require_credentials'), \
                patch.object(replay.subprocess, 'Popen', side_effect=start), \
                patch.object(replay.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stderr='')) as run, \
                patch.object(replay, 'cleanup_replay_containers', return_value=cleanup or []) as clean, \
                patch.object(replay.time, 'sleep', side_effect=AssertionError('Terminal state must not sleep')):
            error = None
            try:
                code = replay.monitor(args)
            except RuntimeError as exc:
                code, error = None, str(exc)
        clean.assert_called_once_with(args.output)
        commands = [call.args[0] for call in run.call_args_list]
        return args.output, code, error, commands, children

    def test_completed_workers_exit_without_kill_and_write_cleanup(self):
        output, code, error, commands, children = self.run_monitor({'trace2skill': 'completed', 'reasoningbank': 'completed'})
        self.assertEqual(code, 0)
        self.assertIsNone(error)
        self.assertEqual(read_json(output / 'monitor.json')['status'], 'completed')
        self.assertEqual(read_json(output / 'container-cleanup.json'), [])
        self.assertFalse(any(cmd[0] == 'taskkill' for cmd in commands))

    def test_failed_worker_stops_only_its_live_sibling_and_preserves_failure(self):
        output, code, error, commands, children = self.run_monitor({'trace2skill': 'failed', 'reasoningbank': 'live'})
        self.assertIn('Replay worker failed', error)
        self.assertEqual(read_json(output / 'monitor.json')['status'], 'failed')
        self.assertTrue((output / 'monitor-error.json').exists())
        self.assertEqual([cmd for cmd in commands if cmd[0] == 'taskkill'],
                         [['taskkill', '/PID', str(children[1].pid), '/T', '/F']])
        self.assertIsNotNone(children[1].poll())

    def test_cleanup_failure_is_reported_even_after_successful_workers(self):
        output, code, error, commands, children = self.run_monitor(
            {'trace2skill': 'completed', 'reasoningbank': 'completed'},
            cleanup=[{'id': 'owned', 'removed': False}])
        self.assertIn('owned-container cleanup failed', error)
        self.assertEqual(read_json(output / 'monitor.json')['status'], 'failed')
        self.assertFalse(read_json(output / 'container-cleanup.json')[0]['removed'])


if __name__ == '__main__':
    unittest.main()
