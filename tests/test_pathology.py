import json
import unittest
from pathlib import Path

from rsi.skills.trace2skill.pathology import PathologyTracker, default_is_edit, detect, load_rollout_steps


def step(number, command=None, sha='a' * 64, truncated=False, format_failed=False, error_type=None, output=None):
    item = {'step': number, 'command': command, 'truncated': truncated, 'format_failed': format_failed,
            'error_type': error_type}
    if output is not None:
        item['output'] = output
    else:
        item['output_sha256'] = sha
    return item


class RepeatTests(unittest.TestCase):
    def test_same_command_same_sha_without_an_edit_is_repeated(self):
        steps = [step(1, 'grep show_default docs | cat'), step(2, 'ls'),
                 step(3, 'grep show_default docs | cat'), step(4, 'grep show_default docs | cat')]
        found = [item for item in detect(steps, k1=3) if item['type'] == 'repeated_command']
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]['start_step'], 1)
        self.assertEqual(found[0]['end_step'], 4)
        self.assertEqual(found[0]['evidence']['count'], 3)
        self.assertEqual(found[0]['evidence']['output_sha256'], 'a' * 64)

    def test_fewer_than_k1_does_not_trigger(self):
        steps = [step(1, 'grep show_default docs | cat'), step(2, 'grep show_default docs | cat')]
        self.assertFalse([item for item in detect(steps, k1=3) if item['type'] == 'repeated_command'])

    def test_different_output_sha_does_not_trigger(self):
        steps = [step(1, 'grep x', sha='a'), step(2, 'grep x', sha='b'), step(3, 'grep x', sha='c')]
        self.assertFalse([item for item in detect(steps, k1=3) if item['type'] == 'repeated_command'])

    def test_edit_between_occurrences_breaks_the_chain(self):
        steps = [step(1, 'grep x'), step(2, 'cat > src/a.py <<EOF\nprint(1)\nEOF'),
                 step(3, 'grep x'), step(4, 'sed -i s/a/b/ src/a.py'), step(5, 'grep x')]
        self.assertFalse([item for item in detect(steps, k1=3) if item['type'] == 'repeated_command'])

    def test_k1_boundary(self):
        steps = [step(1, 'grep x'), step(2, 'grep x')]
        self.assertEqual(len([item for item in detect(steps, k1=2) if item['type'] == 'repeated_command']), 1)
        self.assertFalse([item for item in detect(steps, k1=3) if item['type'] == 'repeated_command'])

    def test_missing_sha_is_hashed_from_output_text(self):
        steps = [step(1, 'grep x', output='same'), step(2, 'grep x', output='same'), step(3, 'grep x', output='same')]
        found = [item for item in detect(steps, k1=3) if item['type'] == 'repeated_command']
        self.assertEqual(len(found), 1)
        self.assertEqual(len(found[0]['evidence']['output_sha256']), 64)


class OtherPathologyTests(unittest.TestCase):
    def test_truncation_and_format_thresholds(self):
        truncated = [step(index, truncated=True, error_type='output_truncated') for index in range(1, 4)]
        self.assertFalse([item for item in detect(truncated[:2], k2=3) if item['type'] == 'truncation_loop'])
        self.assertEqual(len([item for item in detect(truncated, k2=3) if item['type'] == 'truncation_loop']), 1)
        broken = truncated[:2] + [step(3, 'ls')] + truncated[:2]
        self.assertFalse([item for item in detect(broken, k2=3) if item['type'] == 'truncation_loop'])
        failed = [step(index, format_failed=True, error_type='missing_or_unclosed_bash_block') for index in range(1, 4)]
        self.assertFalse([item for item in detect(failed[:2], k3=3) if item['type'] == 'format_death'])
        found = [item for item in detect(failed, k3=3) if item['type'] == 'format_death']
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]['evidence']['error_types'], ['missing_or_unclosed_bash_block'])

    def test_zero_edit_stall_uses_a_strict_half(self):
        reads = [step(index, 'grep x') for index in range(1, 6)]
        self.assertFalse([item for item in detect(reads, step_limit=10) if item['type'] == 'zero_edit_stall'])
        reads.append(step(6, 'grep y'))
        found = [item for item in detect(reads, step_limit=10) if item['type'] == 'zero_edit_stall']
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]['evidence']['steps'], 6)
        reads[-1] = step(6, 'git apply fix.patch')
        self.assertFalse([item for item in detect(reads, step_limit=10) if item['type'] == 'zero_edit_stall'])

    def test_redirect_and_stderr_redirect_are_distinguished(self):
        self.assertFalse(default_is_edit('python -m pytest tests/ -q 2>&1 | tail -5'))
        self.assertTrue(default_is_edit('cat > src/click/core.py <<EOF\nx\nEOF'))
        self.assertTrue(default_is_edit('sed -i s/a/b/ src/a.py'))
        self.assertFalse(default_is_edit('grep show_default docs | cat'))

    def test_python_heredoc_open_write_breaks_a_same_sha_chain(self):
        read = "cd /testbed && nl -ba src/click/core.py | sed -n '2836,2847p'"
        heredoc = """cd /testbed && python - <<'EOF'
path = "src/click/core.py"
src = open(path).read()
open(path, "w").write(src)
print("patched")
EOF"""
        steps = [step(1, read), step(2, heredoc), step(3, read), step(4, read)]
        self.assertTrue(default_is_edit(heredoc))
        self.assertFalse(default_is_edit('src = open(path).read()'))
        self.assertFalse([item for item in detect(steps, k1=3) if item['type'] == 'repeated_command'])
        self.assertTrue(default_is_edit('Path("src/click/core.py").write_bytes(b"x")'))
        self.assertTrue(default_is_edit('shutil.copy(src, dst)'))
        self.assertTrue(default_is_edit('shutil.copyfile(src, dst)'))
        self.assertTrue(default_is_edit('shutil.move(src, dst)'))
        self.assertTrue(default_is_edit('os.rename(src, dst)'))
        self.assertTrue(default_is_edit('os.replace(src, dst)'))


def _event_key(event):
    return event['type'], event['start_step'], event['end_step'], event['count']


def _finding_key(finding):
    evidence = finding['evidence']
    return finding['type'], finding['start_step'], finding['end_step'], evidence.get('count', evidence.get('steps'))


class IncrementalTests(unittest.TestCase):
    def assert_matches_offline(self, steps, **kwargs):
        tracker = PathologyTracker(**kwargs)
        for index, item in enumerate(steps, start=1):
            events = tracker.observe(item.get('command'), item.get('output_sha256'),
                                     item.get('truncated', False), item.get('format_failed', False))
            offline = [found for found in detect(steps[:index], **kwargs) if found['end_step'] == index]
            self.assertEqual([_event_key(event) for event in events], [_finding_key(found) for found in offline])

    def test_prefixes_match_the_offline_scan(self):
        read = "cd /testbed && nl -ba src/click/core.py | sed -n '2836,2847p'"
        heredoc = """cd /testbed && python - <<'EOF'
path = "src/click/core.py"
src = open(path).read()
open(path, "w").write(src)
print("patched")
EOF"""
        sequences = [
            [step(1, 'grep x'), step(2, 'ls'), step(3, 'grep x'), step(4, 'grep x')],
            [step(1, 'grep x', sha='a'), step(2, 'grep x', sha='b'), step(3, 'grep x', sha='c')],
            [step(1, 'grep x'), step(2, 'cat > src/a.py <<EOF\nx\nEOF'), step(3, 'grep x'), step(4, 'grep x')],
            [step(1, read), step(2, heredoc), step(3, read), step(4, read)],
            [step(index, 'grep x', truncated=True) for index in range(1, 5)],
            [step(index, None, format_failed=True, error_type='missing_or_unclosed_bash_block') for index in range(1, 5)],
            [step(index, 'grep x') for index in range(1, 6)],
        ]
        for steps in sequences:
            self.assert_matches_offline(steps, k1=3, k2=3, k3=3, step_limit=6)
        self.assert_matches_offline(
            [step(1, 'grep x'), step(2, 'EDIT'), step(3, 'grep x'), step(4, 'grep x')],
            k1=3, is_edit=lambda command: command == 'EDIT')

    def test_heredoc_open_write_cuts_the_live_chain(self):
        read = "cd /testbed && nl -ba src/click/core.py | sed -n '2836,2847p'"
        heredoc = """cd /testbed && python - <<'EOF'
open(path, "w").write(src)
EOF"""
        tracker = PathologyTracker(k1=3)
        events = []
        for command in (read, heredoc, read, read):
            events.extend(tracker.observe(command, 'a' * 64, False, False))
        self.assertFalse([event for event in events if event['type'] == 'repeated_command'])
        self.assertEqual(len(tracker.chains[read]), 2)


class LoaderTests(unittest.TestCase):
    def test_protocol_and_command_index_join_on_assistant_message(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            rollout = Path(tmp) / 'rollout-0'
            commands = rollout / 'commands'
            commands.mkdir(parents=True)
            (commands / 'index.jsonl').write_text(json.dumps({
                'sequence': 1, 'assistant_message': 2, 'command': 'grep x',
                'output': {'sha256': 'abc'}}) + '\n', encoding='utf-8')
            (rollout / 'action-protocol.jsonl').write_text('\n'.join([
                json.dumps({'model_call': 1, 'assistant_message': 2, 'finish_reason': 'stop',
                            'error_type': None, 'executed': True}),
                json.dumps({'model_call': 2, 'assistant_message': 4, 'finish_reason': 'length',
                            'error_type': 'output_truncated', 'executed': False}),
            ]) + '\n', encoding='utf-8')
            steps = load_rollout_steps(rollout)
        self.assertEqual([item['step'] for item in steps], [1, 2])
        self.assertEqual(steps[0]['command'], 'grep x')
        self.assertEqual(steps[0]['output_sha256'], 'abc')
        self.assertTrue(steps[1]['truncated'])
        self.assertFalse(steps[1]['format_failed'])


if __name__ == '__main__':
    unittest.main()
