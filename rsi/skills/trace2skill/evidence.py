"""Read archived observations and inspect a disposable patched SWE repository."""
import base64
import json
import shlex
import subprocess
from pathlib import Path, PurePosixPath
from rsi.core.common import write_json


class EvidenceReader:
    def __init__(self, task, result, grade, folder):
        self.task, self.result, self.grade, self.folder = task, result, grade, Path(folder)
        self.env = None
        self.events = []

    def record(self, name, arguments, result):
        self.events.append({'tool': name, 'arguments': arguments, 'result': result})
        write_json(self.folder / 'tools.json', self.events)
        return json.dumps(result, ensure_ascii=False)

    def read_evidence(self, kind: str, start: int = 0, chars: int = 10000) -> str:
        """Read trajectory, patch, grade, or command:N output by character offset."""
        if start < 0 or not 1 <= chars <= 12000:
            raise ValueError('start must be nonnegative; chars must be 1..12000')
        values = {'trajectory': self.result['messages'], 'patch': self.result['artifact'], 'grade': self.grade}
        if kind.startswith('command:'):
            number = int(kind.split(':', 1)[1])
            commands = self.folder.parent.parent / f"rollout-{self.result['rollout']}" / 'commands'
            record = json.loads((commands / f'{number:06d}.json').read_text(encoding='utf-8'))
            name = record.get('output', {}).get('path')
            if not name or Path(name).name != name:
                raise ValueError('Command has no safe archived output path')
            value = (commands / name).read_text(encoding='utf-8', errors='replace')
        elif kind in values:
            value = values[kind]
        else:
            raise ValueError('Unknown evidence kind')
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        return self.record('read_evidence', {'kind': kind, 'start': start, 'chars': chars},
                           {'text': text[start:start + chars], 'total_chars': len(text),
                            'next_offset': min(start + chars, len(text))})

    def repository(self):
        if self.task['benchmark'] not in ('swe', 'swe-evo'):
            raise ValueError('Repository probing is available for SWE tasks only')
        if self.env is None:
            from rsi.benchmarks.backends import CompatibleDockerEnvironment
            instance = self.task['instance']
            iid = instance['instance_id'].replace('__', '_1776_')
            image = instance.get('image_name') or instance.get('image') or f'swebench/sweb.eval.x86_64.{iid}:latest'.lower()
            self.env = CompatibleDockerEnvironment(image=image, cwd='/testbed', timeout=30,
                container_timeout='30m', pull_timeout=120,
                run_args=['--rm', '--network', 'none', '--memory', '512m', '--cpus', '1', '--pids-limit', '128'])
            write_json(self.folder / 'probe-container.json', {'container_id': self.env.container_id})
            actual = self.env.execute('git rev-parse HEAD')
            if actual['returncode'] or actual['output'].strip() != instance['base_commit']:
                raise RuntimeError('Analysis repository base commit mismatch')
            payload = base64.b64encode(self.result['artifact'].encode()).decode()
            if self.result['artifact'].strip():
                applied = self.env.execute(f'printf %s {shlex.quote(payload)} | base64 -d | git apply --whitespace=nowarn -')
                if applied['returncode']:
                    raise RuntimeError('Cannot apply the graded patch to analysis repository: ' + applied['output'])
        return self.env

    def read_source(self, path: str, start_line: int = 1, lines: int = 100) -> str:
        """Read a repository file from the graded patch's disposable working tree."""
        p = PurePosixPath(path)
        if not path or p.is_absolute() or '..' in p.parts or '\\' in path or start_line < 1 or not 1 <= lines <= 200:
            raise ValueError('Require a repository-relative path and at most 200 lines')
        result = self.repository().execute(f"sed -n '{start_line},{start_line + lines - 1}p' -- {shlex.quote(path)}")
        return self.record('read_source', {'path': path, 'start_line': start_line, 'lines': lines}, result)

    def run_probe(self, code: str) -> str:
        """Execute a small Python diagnostic in the disposable patched SWE container, never on the host."""
        if not code.strip() or len(code) > 12000:
            raise ValueError('Probe must contain 1..12000 characters')
        payload = base64.b64encode(code.encode()).decode()
        script = f"import base64; exec(compile(base64.b64decode('{payload}'),'<skill-probe>','exec'))"
        result = self.repository().execute('python -c ' + shlex.quote(script))
        raw = result.get('output', '')
        result = {**result, 'output': raw[:12000], 'output_chars': len(raw), 'truncated': len(raw) > 12000}
        return self.record('run_probe', {'code': code}, result)

    def diagnostic_patch(self):
        """Export source changes relative to the task base.

        Probe runs such as pytest leave bytecode and test caches in the disposable
        tree. Those files are removed before staging so the export stays a source
        patch and can finish inside the analysis container.
        """
        if self.env is None:
            return self.result['artifact']
        base = self.task['instance']['base_commit']
        # find -prune removes each generated directory without walking its contents.
        cleanup = (
            "find . -type d \\( -name __pycache__ -o -name .pytest_cache -o -name .mypy_cache "
            "-o -name .ruff_cache -o -name htmlcov -o -name '*.egg-info' \\) -prune -exec rm -rf {} + ; "
            "find . -type f \\( -name '*.pyc' -o -name '*.pyo' -o -name .coverage \\) -delete"
        )
        excludes = ' '.join(
            "':(exclude)" + pattern + "'"
            for pattern in ('**/__pycache__/**', '**/*.pyc', '**/*.pyo', '**/.pytest_cache/**',
                            '**/.mypy_cache/**', '**/.ruff_cache/**', '**/htmlcov/**', '**/.coverage',
                            '**/*.egg-info/**'))
        command = f'{cleanup} && git add -A -- . {excludes} && git diff --cached --binary ' + shlex.quote(base)
        previous = self.env.config.timeout
        self.env.config.timeout = max(previous, 120)
        try:
            result = self.env.execute(command)
        finally:
            self.env.config.timeout = previous
        if result['returncode']:
            raise RuntimeError('Cannot export final diagnostic patch: ' + result['output'])
        return result['output']

    def close(self):
        if self.env is not None and self.env.container_id:
            result = subprocess.run([self.env.config.executable, 'rm', '-f', self.env.container_id],
                                    capture_output=True, text=True, timeout=60)
            if result.returncode:
                raise RuntimeError('Cannot clean analysis container: ' + result.stderr)
            self.env.container_id = None
            write_json(self.folder / 'probe-cleanup.json', {'removed': True})
