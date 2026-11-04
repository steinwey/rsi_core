import json
from pathlib import Path
import tempfile
import unittest

from rsi.core.common import write_json
from scripts.watch_trajectory_steps import trajectory_counters


class TrajectoryStepWatcherTests(unittest.TestCase):
    def test_live_lower_bound_final_counts_and_queued_routes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / 'rollout-0/action-protocol.jsonl'
            path.parent.mkdir()
            path.write_text(json.dumps({'model_call': 121, 'total_failures': 2}) + '\n{"partial":', encoding='utf-8')
            write_json(root / 'rollout-0/action-repair/call-1.request.json', {})
            write_json(root / 'rollout-1/trajectory.json', {
                'status': 'Submitted', 'usage': {'calls': 80}, 'messages': [{'role': 'assistant'}] * 82,
                'executed_commands': 75, 'repair_attempts': 2, 'seconds': 10, 'error': None})
            rows = trajectory_counters(root, 5)
            self.assertEqual(rows[0]['solver_calls'], 121)
            self.assertFalse(rows[0]['solver_calls_are_final'])
            self.assertEqual(rows[0]['format_repair_requests'], 1)
            self.assertEqual(rows[1]['solver_calls'], 80)
            self.assertEqual(rows[1]['assistant_steps'], 82)
            self.assertEqual(rows[1]['executed_commands'], 75)
            self.assertTrue(rows[1]['solver_calls_are_final'])
            self.assertEqual(rows[2]['status'], 'queued')
            self.assertEqual(trajectory_counters(root, 5, terminal=True)[0]['status'], 'interrupted')
