import errno
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from rsi.core.common import read_json, write_json
from scripts.monitor_experiment import publish_monitor_status


def sharing_error():
    exc = PermissionError('Windows file temporarily in use')
    exc.winerror = 5
    return exc


class AtomicStatusTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Real Windows file-sharing regression')
    def test_real_open_reader_releases_and_atomic_publish_recovers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / 'monitor.json'
            write_json(path, {'generation': 'old'})
            reader = path.open('r', encoding='utf-8')
            # Reproduce the original failure without relying on process timing.
            probe = root / 'probe.tmp'
            probe.write_text('{}')
            with self.assertRaises(PermissionError):
                probe.replace(path)
            probe.unlink()
            release = threading.Timer(0.2, reader.close)
            release.start()
            try:
                failures = publish_monitor_status(path, (root / 'launch.json',),
                    {'status': 'running'}, {'pid': 123}, 0)
            finally:
                reader.close()
                release.join()
            self.assertEqual(failures, 0)
            self.assertEqual(read_json(path)['status'], 'running')
            self.assertEqual(read_json(root / 'launch.json')['pid'], 123)
            self.assertEqual(list(root.glob('.rsi-*.tmp')), [])

    def test_persistent_lock_keeps_old_checkpoint_and_removes_temporary_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / 'checkpoint.json'
            write_json(path, {'completed': []})
            before = path.read_bytes()
            with patch.object(Path, 'replace', side_effect=sharing_error()) as replace, \
                 patch('rsi.core.common.time.sleep'):
                with self.assertRaises(PermissionError):
                    write_json(path, {'completed': ['must not be committed']})
                self.assertEqual(replace.call_count, 6)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(root.glob('.rsi-*.tmp')), [])

    def test_single_blocked_snapshot_does_not_abort_and_next_poll_recovers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            status, launch = {'status': 'running'}, {'pid': 123}
            with patch('scripts.monitor_experiment.write_json', side_effect=sharing_error()), patch('builtins.print'):
                failures = publish_monitor_status(root / 'monitor.json', (root / 'launch.json',), status, launch, 0)
            self.assertEqual(failures, 1)
            self.assertEqual(status['status'], 'running')
            failures = publish_monitor_status(root / 'monitor.json', (root / 'launch.json',), status, launch, failures)
            self.assertEqual(failures, 0)
            self.assertNotIn('status_write_error', read_json(root / 'monitor.json'))

    def test_three_blocked_snapshot_polls_still_fail_closed(self):
        with patch('scripts.monitor_experiment.write_json', side_effect=sharing_error()), patch('builtins.print'):
            status, launch = {'status': 'running'}, {'pid': 123}
            failures = 0
            for expected in (1, 2):
                failures = publish_monitor_status(Path('monitor.json'), (), status, launch, failures)
                self.assertEqual(failures, expected)
            with self.assertRaises(PermissionError):
                publish_monitor_status(Path('monitor.json'), (), status, launch, failures)

    def test_unrelated_io_errors_are_not_suppressed_or_retried(self):
        error = OSError(errno.ENOSPC, 'No space left on device')
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(Path, 'replace', side_effect=error) as replace, patch('rsi.core.common.time.sleep') as sleep:
                with self.assertRaises(OSError):
                    write_json(Path(tmp) / 'checkpoint.json', {})
                self.assertEqual(replace.call_count, 1)
                sleep.assert_not_called()
        with patch('scripts.monitor_experiment.write_json', side_effect=error):
            with self.assertRaises(OSError):
                publish_monitor_status(Path('monitor.json'), (), {'status': 'running'}, {}, 0)
