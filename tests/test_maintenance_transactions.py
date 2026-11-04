import contextlib
import io
import json
import shlex
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from rsi.agent.runner import run_experiment
from rsi.benchmarks.backends import install_skill, HarborSyncEnvironment
from rsi.core.common import digest, read_json, write_json
from rsi.experiment import checkpoints
from rsi.skills.directory import INDEX_PATH, read_skill_directory
from rsi.skills.index import build_index
from rsi.skills.store import SkillStore, load_initial_skill, SkillProposalRejected
from rsi.skills.trace2skill.pipeline import guard_editor_writes
from test_core_baseline import Backend, Environment, Models, test_config


class LocalEnvironment:
    """Execute only installer Python against a disposable substitute for /opt."""
    def __init__(self, root):
        self.root = root

    def execute(self, command, cwd=''):
        if command.startswith('mkdir '):
            self.root.mkdir(parents=True, exist_ok=True)
            return {'returncode': 0, 'output': ''}
        words = shlex.split(command)
        assert words[:2] in (['python', '-c'], ['python3', '-c'])
        script = words[2].replace("'/opt/rsi-skills'", repr(str(self.root)))
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            exec(script, {})
        return {'returncode': 0, 'output': stream.getvalue()}


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.seed, _ = load_initial_skill(directory_schema=1, bootstrap=True)
        self.store = SkillStore(self.root / 'store', self.seed, directory_schema=1)

    def test_existing_version_verified_not_overwritten(self):
        folder = self.store.current()
        self.assertEqual(self.store.materialize(self.seed), folder.name)
        (folder / 'SKILL.md').write_text('corrupt', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.store.materialize(self.seed)
        self.assertEqual((folder / 'SKILL.md').read_text(), 'corrupt')

    def test_write_failure_has_no_partial_version_or_pointer_change(self):
        old = self.store.current()
        files = self.seed | {'SKILL.md': self.seed['SKILL.md'] + '\nExtra\n'}
        real_open = Path.open
        def fail(path, *args, **kwargs):
            if any(p.startswith('.staging-') for p in path.parts) and path.name == 'index.yaml':
                raise OSError('Injected write failure')
            return real_open(path, *args, **kwargs)
        with patch.object(Path, 'open', fail), self.assertRaises(OSError):
            self.store.materialize(files)
        self.assertEqual(self.store.current(), old)
        self.assertFalse((old.parent / digest(files)).exists())
        self.assertFalse(list(old.parent.glob('.staging-*')))

    def test_publish_directory_failure_and_windows_retry(self):
        old = self.store.current()
        files = self.seed | {'SKILL.md': self.seed['SKILL.md'] + '\nExtra\n'}
        with patch.object(Path, 'rename', side_effect=OSError('occupied')), self.assertRaises(OSError):
            self.store.materialize(files)
        self.assertEqual(self.store.current(), old)
        original = Path.rename
        count = 0
        def sharing(path, target):
            nonlocal count
            count += 1
            if count == 1:
                exc = PermissionError('sharing violation')
                exc.winerror = 32
                raise exc
            return original(path, target)
        with patch.object(Path, 'rename', sharing):
            version = self.store.materialize(files)
        self.assertEqual(version, digest(files))
        self.assertEqual(count, 2)

    def test_fixed_tool_change_rejected_before_writes(self):
        with self.assertRaises(ValueError):
            self.store.materialize(self.seed | {'scripts/inspect_project.py': 'changed'})
        self.assertEqual(len(list((self.root / 'store/versions').iterdir())), 1)

    def test_editor_batch_rejected_before_any_write(self):
        calls = []
        evolver = SimpleNamespace(apply_edits=lambda edits: calls.extend(edits))
        guard_editor_writes(evolver, self.store.current(), 1)
        good = SimpleNamespace(relative_path='SKILL.md', content='good', action='modify')
        for name in ('../escape.md', INDEX_PATH, 'scripts/inspect_project.py', 'references/../escape.md'):
            with self.subTest(name=name), self.assertRaises(SkillProposalRejected):
                evolver.apply_edits([good, SimpleNamespace(relative_path=name, content='bad', action='create')])
        self.assertEqual(calls, [])
        evolver.apply_edits([good])
        self.assertEqual(calls, [good])

    def test_install_fallback_exact_snapshot_and_detect_extra(self):
        target = self.root / 'installed'
        env = LocalEnvironment(target)
        install_skill(env, self.store.current(), directory_schema=1)
        self.assertEqual(read_skill_directory(target, directory_schema=1), self.seed)
        (target / 'extra.md').write_text('stale')
        with self.assertRaisesRegex(RuntimeError, 'differs'):
            install_skill(env, self.store.current(), directory_schema=1)

    def test_install_detects_adapter_omitting_index(self):
        target = self.root / 'bad-adapter'
        env = LocalEnvironment(target)
        def copy_without_index(folder):
            shutil.copytree(folder, target)
            (target / INDEX_PATH).unlink()
        env.install_skill = copy_without_index
        with self.assertRaisesRegex(RuntimeError, 'differs'):
            install_skill(env, self.store.current(), directory_schema=1)

    def test_docker_copy_and_harbor_upload_include_index(self):
        target = self.root / 'docker'
        env = LocalEnvironment(target)
        env.container_id = 'fake'
        env.config = SimpleNamespace(executable='docker')
        def docker_copy(command, **kwargs):
            shutil.copytree(command[2].removesuffix('/.'), target, dirs_exist_ok=True)
        with patch('rsi.benchmarks.backends.subprocess.run', side_effect=docker_copy):
            install_skill(env, self.store.current(), directory_schema=1)
        self.assertEqual(read_skill_directory(target, directory_schema=1), self.seed)
        target = self.root / 'harbor'
        class Raw:
            async def upload_dir(self, source_dir, target_dir):
                shutil.copytree(source_dir, target)
        import asyncio
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)
        harbor = HarborSyncEnvironment(Raw(), loop, 10)
        local = LocalEnvironment(target)
        harbor.execute = local.execute
        # mkdir precedes upload; fake upload supports an existing target.
        async def upload(source_dir, target_dir):
            shutil.copytree(source_dir, target, dirs_exist_ok=True)
        harbor.env.upload_dir = upload
        install_skill(harbor, self.store.current(), directory_schema=1)
        self.assertEqual(read_skill_directory(target, directory_schema=1), self.seed)


class ManagedCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.output = Path(self.tmp.name) / 'run'
        self.config = test_config() | {'skill_directory': {'schema': 1}}
        self.tasks = [{'id': 'task', 'benchmark': 'swe', 'prompt': 'Fix visible issue'}]
        self.models = Models()
        Backend.barrier, Backend.instances = threading.Barrier(5), []
        for target, value in [('rsi.agent.runner.functional_gate', {'passed': True})]:
            mock = patch(target, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)
        # Installation itself is covered above; integration fake returns a real manifest.
        original = Environment.execute
        def execute(env, command, cwd=''):
            if 'RSI_MANIFEST=' in command:
                import hashlib
                files = env.skill
                hashes = {p: hashlib.sha256(v.encode()).hexdigest() for p, v in files.items()}
                return {'returncode': 0, 'output': 'RSI_MANIFEST=' + json.dumps(hashes)}
            return original(env, command, cwd)
        mock = patch.object(Environment, 'execute', execute)
        mock.start()
        self.addCleanup(mock.stop)
        mock = patch.object(Environment, 'install_skill', lambda env, folder: setattr(env, 'skill', read_skill_directory(folder, directory_schema=1)), create=True)
        mock.start()
        self.addCleanup(mock.stop)

    def run_tasks(self, resume=False):
        return run_experiment(self.tasks, self.config, self.output, resume=resume,
                              backend_factory=Backend, model_factory=self.models)

    def test_commit_then_publish_failure_resume_no_repeated_calls(self):
        with patch('rsi.experiment.checkpoints.publish', side_effect=OSError('publish failed')), self.assertRaises(OSError):
            self.run_tasks()
        state, config, _ = checkpoints.resume_request(self.output)
        self.assertEqual((state['schema'], state['directory_schema']), (6, 1))
        self.assertEqual(len(state['completed']), 1)
        self.assertNotEqual(read_json(self.output / 'skills/current.json')['version'], state['current_skill_version'])
        self.models.requests.clear()
        self.run_tasks(resume=True)
        self.assertEqual(self.models.requests, [])
        self.assertEqual(read_json(self.output / 'skills/current.json')['version'], state['current_skill_version'])
        self.run_tasks(resume=True)
        self.assertEqual(len(read_json(self.output / 'checkpoint.json')['completed']), 1)

    def test_before_commit_failure_preserves_pointer_and_resume_archives(self):
        with patch('rsi.agent.runner.commit', side_effect=OSError('before commit')), self.assertRaises(OSError):
            self.run_tasks()
        state, _, _ = checkpoints.resume_request(self.output)
        self.assertEqual(state['completed'], [])
        self.assertEqual(read_json(self.output / 'skills/current.json')['version'], state['initial_skill_version'])
        self.run_tasks(resume=True)
        self.assertEqual(len(list((self.output / 'attempts').iterdir())), 1)
        self.assertEqual(len(read_json(self.output / 'checkpoint.json')['completed']), 1)

    def test_schema6_tamper_and_duplicate_commit_rejected(self):
        self.run_tasks()
        state, _, _ = checkpoints.resume_request(self.output)
        entry = state['completed'][0]
        with self.assertRaises(ValueError):
            checkpoints.commit(self.output, state, entry['report'], entry['update'])
        path = self.output / 'skills/versions' / state['current_skill_version'] / INDEX_PATH
        path.write_text('schema_version: 1\nentries: []\n# modified\n')
        with self.assertRaises(ValueError):
            checkpoints.resume_request(self.output)

    def test_actual_editor_reference_creation_and_five_route_inheritance(self):
        class ReferenceModels(Models):
            def __call__(owner, config):
                client = super().__call__(config)
                original = client.query
                def respond(messages, **kwargs):
                    answer = original(messages, **kwargs)
                    if client.name == 'judge' and answer['content'].startswith(chr(96)*3 + 'json'):
                        data = json.loads(answer['content'][7:-3])
                        data['edits'] = [
                            {'file': 'SKILL.md', 'op': 'append_to_section', 'target_section': '# Test and diagnose',
                             'content': 'Read [contracts](references/contracts.md).'},
                            {'file': 'references/contracts.md', 'op': 'create', 'content': '# Contracts\nCheck observable behavior.\n'}]
                        answer['content'] = chr(96)*3 + 'json\n' + json.dumps(data) + '\n' + chr(96)*3
                    return answer
                client.query = respond
                return client
        self.models = ReferenceModels()
        self.tasks.append({'id': 'second', 'benchmark': 'swe', 'prompt': 'Fix another visible issue'})
        reports = self.run_tasks()
        version = read_json(self.output / 'task/update.json')['after_skill_version']
        self.assertEqual(reports[1]['skill_version'], version)
        from rsi.skills.index import validate_index
        files = read_skill_directory(self.output / 'skills/versions' / version, directory_schema=1)
        self.assertEqual([e['path'] for e in validate_index(files)['entries']], ['references/contracts.md'])
        for i in range(5):
            route = read_json(self.output / f'second/rollout-{i}/trajectory.json')
            self.assertEqual(route['skill_version'], version)
            self.assertEqual(route['tools_hash'], read_json(self.output / 'initial-skill-source.json')['tools_hash'])

    def test_schema6_cannot_be_downgraded_or_schema5_upgraded(self):
        self.run_tasks()
        path = self.output / 'checkpoint.json'
        state = read_json(path)
        write_json(path, state | {'schema': 5})
        with self.assertRaisesRegex(ValueError, 'mismatch'):
            checkpoints.resume_request(self.output)
        write_json(path, state | {'directory_schema': 0})
        with self.assertRaisesRegex(ValueError, 'mismatch'):
            checkpoints.resume_request(self.output)

    def test_schema5_without_new_config_fields_resumes_unchanged(self):
        for name in ('skill_directory', 'skill_maintenance', 'reference_maintenance', 'skill_routing'):
            self.config.pop(name, None)
        self.run_tasks()
        state, _, _ = checkpoints.resume_request(self.output)
        self.assertEqual(state['schema'], 5)
        self.assertNotIn('directory_schema', state)
        before = (self.output / 'checkpoint.json').read_bytes()
        self.run_tasks(resume=True)
        self.assertEqual((self.output / 'checkpoint.json').read_bytes(), before)
