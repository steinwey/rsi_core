"""Run one experiment with a local watchdog; never restart a failed run."""
import json
import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rsi import ROOT
from rsi.core.config import apply_benchmark_execution, load_benchmark_config, load_config
from rsi.core.common import write_json as atomic_write_json, transient_file_access_error

DEFAULT_BENCHMARK_CONFIG = ROOT / 'configs/benchmark/smoke-five.yaml'
POLL_SECONDS = 15
MIN_FREE_GIB = 2
STORAGE_SECONDS = 60
DISK_WARNING_SECONDS = 120
CONTAINER_LIST_FORMAT = '{"ID":{{json .ID}},"Names":{{json .Names}}}'
DOCKER_VHDX = Path('D:/Docker/wsl/disk/docker_data.vhdx')
DOCKER_LOG = Path.home() / 'AppData/Local/Docker/log/vm/init.log'
IDLE_SECONDS = 1200
SCORING_IDLE_SECONDS = 3000
MONITORED_PATTERNS = ('*/fixed-tools-check.json', '*/evo-*/score.json', '*/evo-*/update.json',
                      '*/skill-maintenance/*/update.json',
                      '*/evo-*/rollout-*/trajectory.json', '*/evo-*/trace2skill/summary.json',
                      '*/evo-*/reasoningbank/summary.json')


def now():
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec='seconds')


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return None


def manifest_count(path):
    tasks = read_json(path)
    if not isinstance(tasks, list) or not tasks:
        raise ValueError('Monitored task manifests must contain nonempty lists')
    return len(tasks)


def write_json(path, value):
    atomic_write_json(path, value)


def publish_monitor_status(status_path, launch_paths, status, launch, failures):
    """Tolerate a short snapshot lock; fail closed after three blocked polls."""
    status['checked_at'] = now()
    status.pop('status_write_error', None)
    status['status_write_failures'] = 0
    launch['status'] = status['status']
    try:
        write_json(status_path, status)
        for path in launch_paths:
            write_json(path, launch)
    except OSError as exc:
        if not transient_file_access_error(exc):
            raise
        failures += 1
        status.update(status_write_error=str(exc), status_write_failures=failures)
        if failures >= 3:
            raise
        print(f'WARNING: monitor snapshot temporarily blocked ({failures}/3); continuing checks and retrying publication.', flush=True)
        return failures
    return 0


def tail(path, size=65536):
    try:
        with Path(path).open('rb') as stream:
            stream.seek(max(0, stream.seek(0, 2) - size))
            return stream.read().decode('utf-8', errors='replace')
    except OSError:
        return ''


def disk_sample(samples, stamp, free_bytes):
    """Stop only below the byte floor; a rolling-minute forecast is advisory."""
    samples.append((stamp, free_bytes))
    while len(samples) > 1 and samples[0][0] < stamp - 60:
        samples.pop(0)
    elapsed = stamp - samples[0][0]
    consumed = samples[0][1] - free_bytes
    rate = consumed / elapsed if elapsed >= 15 and len(samples) >= 4 and consumed > 0 else 0
    runway = max(0, (free_bytes - MIN_FREE_GIB * 2**30) / rate) if rate else None
    record = {'free_bytes': free_bytes, 'free_gib': round(free_bytes / 2**30, 3),
              'growth_gib_per_minute': round(rate * 60 / 2**30, 3),
              'seconds_to_floor': round(runway, 1) if runway is not None else None,
              'warning': None}
    reason = None
    if free_bytes < MIN_FREE_GIB * 2**30:
        reason = f'Disk free space below {MIN_FREE_GIB} GiB'
    elif runway is not None and runway < DISK_WARNING_SECONDS and consumed >= 0.25 * 2**30:
        record['warning'] = (f'Disk space forecast: estimated {runway:.0f}s until '
                             f'{MIN_FREE_GIB} GiB floor; warning only')
    return record, reason


def storage_fault(started_at, path=DOCKER_LOG):
    """Only new kernel faults count; old failures remain preserved evidence."""
    since = datetime.fromisoformat(started_at)
    for line in tail(path, size=1024 * 1024).splitlines():
        try:
            row = json.loads(line)
            stamp = datetime.fromisoformat(row.get('time', '').replace('Z', '+00:00'))
        except (ValueError, TypeError, AttributeError):
            continue
        message = str(row.get('msg', ''))
        if stamp.tzinfo is not None and stamp >= since and row.get('component') == 'kmsg' and re.search(
                r'Remounting filesystem read-only|Aborting journal on device|EXT4-fs.*I/O error', message, re.I):
            return f'Docker storage fault: {message}'
    return None


def storage_snapshot(output, stdout):
    """Read only this run's container sizes and backing-filesystem availability."""
    record: dict[str, Any] = {'checked_at': now(), 'containers': []}
    owned, grading = owned_containers(output, stdout)

    def query(arguments):
        return subprocess.run(['docker', *arguments], capture_output=True, text=True,
                              encoding='utf-8', errors='replace', timeout=10)

    try:
        # Explicit fields avoid implicitly requesting Size from Docker's formatter.
        inventory = query(['ps', '-a', '--no-trunc', '--format', CONTAINER_LIST_FORMAT])
        if inventory.returncode:
            raise RuntimeError(inventory.stderr.strip() or 'Docker inventory failed')
        targets = [json.loads(line) for line in inventory.stdout.splitlines() if line.strip()]
        targets = [item for item in targets
                   if owned.get(item['Names']) == item['ID'] or item['Names'] in grading]
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        record['health_error'] = f'{type(exc).__name__}: {exc}'
        return record
    if not targets:
        record['writable_bytes'] = 0
        return record
    try:
        result = query(['inspect', '--size', *[item['ID'] for item in targets]])
        details = json.loads(result.stdout) if result.stdout.strip() else []
        if result.returncode and not details:
            raise RuntimeError(result.stderr.strip() or 'Docker size inspection failed')
        # Containers can disappear between inventory and inspect during normal cleanup.
        record['containers'] = [{'id': item['Id'], 'name': item['Name'].lstrip('/'),
                                  'running': item['State']['Running'], 'writable_bytes': item.get('SizeRw')}
                                 for item in details]
        sizes = [item['writable_bytes'] for item in record['containers']]
        record['writable_bytes'] = sum(sizes) if sizes and all(isinstance(x, int) for x in sizes) else None
        running = next((item for item in record['containers'] if item['running']), None)
        if running:
            result = query(['exec', running['id'], 'df', '-B1', '/'])
            if result.returncode:
                raise RuntimeError(result.stderr.strip() or 'Container df failed')
            columns = result.stdout.splitlines()[-1].split()
            record['linux_filesystem'] = dict(zip(('total_bytes', 'used_bytes', 'available_bytes'),
                                                   map(int, columns[1:4])))
    except (OSError, ValueError, KeyError, IndexError, RuntimeError, subprocess.TimeoutExpired) as exc:
        # Size probes can race with build-file deletion; preserve evidence without
        # treating one failed size read as loss of Docker service.
        record['detail_error'] = f'{type(exc).__name__}: {exc}'
    return record


def problem_in(path, data):
    if path.name == 'score.json' and data.get('success') is None:
        return f'Scoring failed: {data.get("error", "missing score")}'
    if path.name == 'trajectory.json':
        if data.get('error'):
            return f'Rollout failed: {data["error"]}'
        assistants = [m.get('content', '') for m in data.get('messages', []) if m.get('role') == 'assistant']
        if (data.get('status') != 'ActionFormatFailed' and len(assistants) >= 3
                and all(not str(m).strip() for m in assistants[-3:])):
            return 'Three consecutive empty solver responses'
    if path.name == 'update.json' and data.get('decision') == 'update_error':
        return f'Skill update failed: {data.get("error")}'
    if path.name == 'fixed-tools-check.json' and data.get('passed') is False:
        return 'Initial fixed-tool check failed'
    if path.parent.name == 'trace2skill' and path.name == 'summary.json' and data.get('status') == 'failed':
        return 'Trace2Skill analysis, merge or application failed; see archived calls'
    if path.parent.name == 'reasoningbank' and path.name == 'summary.json' and data.get('status') == 'failed':
        return 'ReasoningBank aggregation failed; see archived calls'
    return None


def owned_containers(output, stdout):
    """Match exact IDs from this run's log, and exact evaluation directory names."""
    pairs = re.findall(r'Started container (minisweagent-[0-9a-f]+) with ID\s+([0-9a-f]{64})', stdout)
    owned = dict(pairs)
    grading = set()
    for pattern in ('**/logs/evaluation/*/rsi/*',):
        for directory in output.glob(pattern):
            if directory.is_dir():
                grading.add(f'sweb.eval.{directory.name.lower()}.{directory.parent.parent.name}')
    return owned, grading


def cleanup_containers(output, stdout):
    owned, grading = owned_containers(output, stdout)
    # Cleanup must not depend on ps/SizeRw, which can fail while build files vanish.
    targets = list(owned.items()) + [(name, None) for name in sorted(grading)]

    def query(arguments, timeout=20):
        return subprocess.run(['docker', *arguments], capture_output=True, text=True,
                              encoding='utf-8', errors='replace', timeout=timeout)

    def missing(result):
        return result.returncode != 0 and bool(re.search(
            r'No such (?:container|object)', result.stderr or '', re.I))

    def stop(target):
        name, expected_id = target
        record = {'name': name, 'id': expected_id}
        try:
            inspected = query(['container', 'inspect', expected_id or name], timeout=10)
            if missing(inspected):
                record.update(already_removed=True, removed=True)
                return record
            if inspected.returncode:
                raise RuntimeError(inspected.stderr.strip() or 'Container inspection failed')
            container, = json.loads(inspected.stdout)
            cid = container['Id']
            if (container['Name'] != '/' + name or not re.fullmatch(r'[0-9a-f]{64}', cid)
                    or expected_id is not None and cid != expected_id):
                raise ValueError('Container identity differs from this run; refusing cleanup')
            record.update(id=cid, identity_verified=True, running_before=container['State']['Running'])
        except Exception as exc:
            record['inspect_error'] = str(exc)
            return record

        # Always stop the owned container, even when snapshotting fails.
        try:
            if name in owned and record['running_before']:
                directory = output / 'interrupted-containers' / name
                directory.mkdir(parents=True, exist_ok=True)
                snapshot = subprocess.run(['docker', 'exec', cid, 'bash', '-lc',
                                           'cd /testbed && git add -A && git diff --cached HEAD --binary'],
                                          capture_output=True, timeout=20)
                (directory / 'patch.diff').write_bytes(snapshot.stdout)
                (directory / 'snapshot.stderr.txt').write_bytes(snapshot.stderr)
                record['snapshot_exit'] = snapshot.returncode
                record['patch_bytes'] = len(snapshot.stdout)
        except Exception as exc:
            record['snapshot_error'] = str(exc)
        finally:
            try:
                stopped = query(['stop', '--time', '3', cid])
                record['stop_exit'] = stopped.returncode
                if stopped.returncode and not missing(stopped):
                    record['stop_error'] = stopped.stderr.strip() or 'Container stop failed'
            except Exception as exc:
                record['stop_error'] = str(exc)
            # Remove only the verified ID, also when stop timed out. Auto-remove
            # may already have deleted it, which is a successful cleanup.
            try:
                removed = query(['container', 'rm', '--force', cid])
                record['removed'] = removed.returncode == 0 or missing(removed)
                if not record['removed']:
                    record['remove_error'] = removed.stderr.strip() or 'Container removal failed'
            except Exception as exc:
                record['removed'] = False
                record['remove_error'] = str(exc)
        return record

    with ThreadPoolExecutor(max_workers=5) as pool:
        return list(pool.map(stop, targets))


def mark_stopped(output, reason):
    from rsi.experiment.run_logs import update_run
    from rsi.experiment.tracking import summary_metrics
    state = read_json(output / 'workflow.json')
    if state:
        state.update(status='interrupted', error=reason, stopped_at=now())
        phase = state.get('phase')
        if phase in ('train', 'test'):
            state[phase]['status'] = 'interrupted'
            state[phase]['metrics'] = summary_metrics(read_json(output / phase / 'summary.json') or [])
        if state.get('test', {}).get('status') == 'pending':
            state['test']['status'] = 'skipped'
        write_json(output / 'workflow.json', state)
    summaries = []
    for phase in (('train', 'test') if state else ('',)):
        directory = output / phase
        checkpoint = read_json(directory / 'checkpoint.json')
        rows = ([entry['report'] for entry in checkpoint['completed']] if checkpoint
                else read_json(directory / 'summary.json') or [])
        summaries.extend(rows)
        info = read_json(directory / 'run-info.json')
        if phase and info and info.get('status') == 'running':
            update_run(directory, status='interrupted', error=reason, stopped_at=now(), completed_tasks=len(rows))
    if (output / 'run-info.json').exists():
        update_run(output, status='interrupted', error=reason, stopped_at=now(), completed_tasks=len(summaries),
                   scored_tasks=sum(r.get('success') is not None for r in summaries))


def activity_paths(active):
    if not active or not active.exists():
        return []
    return [p for pattern in ('rollout-*/commands/index.jsonl', 'rollout-*/action-protocol.jsonl', 'trace2skill/**/*.json',
                             'reasoningbank/**/*.json',
                             'rollout-*/trajectory.json', 'rollout-*/action-repair/*.json', '*.json',
                             'rollout-*/logs/evaluation/**/run_instance.log') for p in active.glob(pattern)]


def process_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5  # Access denied: conservatively consider it live.
        try:
            code = wintypes.DWORD()
            return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def monitor(resume=None, benchmark_config=None, *, strict=False):
    if resume and benchmark_config is not None:
        raise ValueError('--resume uses saved inputs; do not combine it with --benchmark-config')
    work = ROOT / '_work'
    previous = read_json(work / 'active-parallel-launch.json')
    if previous and previous.get('status') in ('starting', 'running', 'stopping'):
        if process_alive(previous.get('monitor_pid')) or process_alive(previous.get('pid')):
            raise RuntimeError('Another monitor or experiment process is still active')
        if not resume or Path(previous['output']).resolve() != Path(resume).resolve():
            raise RuntimeError('A stale active run exists; explicitly --resume that run before starting another')
    job = datetime.now(timezone(timedelta(hours=8))).strftime('%Y%m%d-%H%M%S') + '__monitored__' + os.urandom(3).hex()
    output = Path(resume).expanduser().resolve() if resume else ROOT / 'runs' / job
    if resume and not output.is_relative_to((ROOT / 'runs').resolve()):
        raise ValueError('Only resume runs within this workspace')
    launch_path = work / (job + '.launch.json')
    status_path = work / (job + '.monitor.json')
    storage_path = work / (job + '.storage.jsonl')
    storage_status = work / (job + '.storage.json')
    stdout_path, stderr_path = work / (job + '.stdout.log'), work / (job + '.stderr.log')
    command = [sys.executable, '-u', '-X', 'utf8', '-m', 'rsi.cli', 'run']
    source = None
    if resume:
        from rsi.experiment.checkpoints import resume_request
        _, settings, groups = resume_request(output)
        from rsi.experiment.checkpoints import run_lock
        # Verify no writer exists before cleaning only this run's recorded containers.
        with run_lock(output):
            for launch_file in work.glob('*.launch.json'):
                old_launch = read_json(launch_file) or {}
                if Path(old_launch.get('output', '.')).resolve() != output:
                    continue
                if process_alive(old_launch.get('pid')) or process_alive(old_launch.get('monitor_pid')):
                    raise RuntimeError('A process from this run is still active')
                cleanup = cleanup_containers(output, tail(Path(old_launch['stdout']), size=16 * 1024 * 1024))
                if any(not row.get('removed') for row in cleanup):
                    raise RuntimeError('Cannot resume until previous run containers are cleaned')
        workflow = False
        training_count = len(groups[0])
        test_count = len(groups[1]) if workflow else 0
        command.extend(['--resume', str(output)])
    else:
        source = Path(benchmark_config or DEFAULT_BENCHMARK_CONFIG).expanduser().resolve()
        options = load_benchmark_config(source)
        if options.get('output') or options.get('runs_dir'):
            raise ValueError('The monitor manages its output directory; omit output and runs_dir from the benchmark YAML')
        settings = load_config(options['config'])
        apply_benchmark_execution(settings, options)
        workflow = bool(options.get('test_tasks'))
        if workflow and (not settings.get('evolve', True) or options.get('fixed_skill')):
            raise ValueError('Automatic train/test requires evolve=true')
        training_count = manifest_count(options['tasks'])
        test_count = manifest_count(options['test_tasks']) if workflow else 0
        command.extend(['--benchmark-config', str(source), '--output', str(output)])
    mode = settings.get('execution_mode', 'parallel')
    phases = ('train', 'test') if workflow else ('',)
    patterns = MONITORED_PATTERNS if workflow else tuple(p.removeprefix('*/') for p in MONITORED_PATTERNS)
    if strict:
        extra = ('evo-*/trace2skill/rollout-*/analysis-error.json',
                 'evo-*/trace2skill/rollout-*/validation.json',
                 'evo-*/trace2skill/final-document-validation.json')
        patterns += tuple('*/' + p if workflow else p for p in extra)
    cleanup_pattern = '*/evo-*/cleanup-errors.log' if workflow else 'evo-*/cleanup-errors.log'
    work.mkdir(exist_ok=True)
    launch = {'status': 'starting', 'started_at': now(), 'output': str(output), 'stdout': str(stdout_path),
              'stderr': str(stderr_path), 'monitor_pid': os.getpid(), 'monitor_status': str(status_path),
              'executable': command[0], 'arguments': command[1:], 'execution_mode': mode,
              'rollouts': settings.get('rollouts', 1 if mode == 'single' else 5),
              'rollout_workers': min(settings.get('rollouts', 5), settings.get('rollout_workers', settings.get('rollouts', 5))),
              'training_tasks': training_count, 'test_tasks': test_count,
              'benchmark_config': str(source) if source else None,
              'resumed_from': str(output) if resume else None,
              'storage_log': str(storage_path), 'storage_status': str(storage_status),
              'monitor_policy': {'poll_seconds': POLL_SECONDS, 'min_free_gib': MIN_FREE_GIB,
                                 'storage_probe_seconds': STORAGE_SECONDS,
                                 'disk_warning_seconds': DISK_WARNING_SECONDS, 'disk_prediction_action': 'warn',
                                 'idle_seconds': IDLE_SECONDS, 'scoring_idle_seconds': SCORING_IDLE_SECONDS,
                                 'strict': strict}}
    process = None
    reason = None
    seen = {p: (p.stat().st_mtime_ns, p.stat().st_size)
            for pattern in patterns for p in output.glob(pattern)} if resume else {}
    previous_cleanup = {p: p.stat().st_mtime_ns for p in output.glob(cleanup_pattern)} if resume else {}
    status: dict[str, Any] = {'status': 'starting', 'started_at': now(), 'output': str(output)}
    disk_samples = []
    storage_pool = ThreadPoolExecutor(max_workers=1)
    storage_future = None
    next_storage = 0
    storage_failures = 0
    previous_completed = None
    disk_warning_active = False
    status_write_failures = 0

    def log_storage(value):
        with storage_path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(value, ensure_ascii=False) + '\n')

    def save(*, required=False):
        nonlocal status_write_failures
        while True:
            status_write_failures = publish_monitor_status(status_path,
                (launch_path, work / 'active-parallel-launch.json'), status, launch, status_write_failures)
            if not status_write_failures:
                return True
            if not required:
                return False
            time.sleep(0.1)

    try:
        with stdout_path.open('wb') as out, stderr_path.open('wb') as err:
            process = subprocess.Popen(command, cwd=ROOT, stdout=out, stderr=err,
                                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            launch['pid'] = process.pid
            status.update(status='running', pid=process.pid, monitor_pid=os.getpid())
            save(required=True)
            print(json.dumps(launch, ensure_ascii=False), flush=True)
            while True:
                stdout = tail(stdout_path)
                stderr = tail(stderr_path)
                state_file = output / ('workflow.json' if workflow else 'run-info.json')
                state = read_json(state_file) or {}
                disk, disk_reason = disk_sample(disk_samples, time.monotonic(), shutil.disk_usage(ROOT).free)
                status.update(phase=state.get('phase', 'preflight' if workflow else 'run'), disk_free_gib=disk['free_gib'])
                status['disk_warning'] = disk.get('warning')
                if status['disk_warning'] and not disk_warning_active:
                    print(f'WARNING: {status["disk_warning"]}', flush=True)
                disk_warning_active = bool(status['disk_warning'])
                checkpoint = read_json(output / 'checkpoint.json')
                status['completed_tasks'] = (len(checkpoint['completed']) if checkpoint else
                    sum(len(read_json(output / p / 'summary.json') or []) for p in phases))
                markers = re.findall(r'\[\d+/\d+\] ([A-Za-z0-9_.-]+)', stdout)
                active_root = output / state.get('phase', 'train') if workflow else output
                active = active_root / markers[-1] if markers else None
                status['current_task'] = markers[-1] if markers else None
                storage = {'checked_at': now(), 'kind': 'host', 'phase': status['phase'],
                           'task': status['current_task'], 'completed_tasks': status['completed_tasks'], **disk}
                if DOCKER_VHDX.exists():
                    storage['vhdx_file_bytes'] = DOCKER_VHDX.stat().st_size
                log_storage(storage)
                status['storage'] = storage
                reason = disk_reason or storage_fault(launch['started_at'])
                if storage_future is not None and storage_future.done():
                    detail = storage_future.result()
                    write_json(storage_status, detail)
                    log_storage({'kind': 'docker', **detail})
                    storage_failures = storage_failures + 1 if detail.get('health_error') else 0
                    status['storage_probe_failures'] = storage_failures
                    storage_future = None
                    if not reason and storage_failures >= 3:
                        reason = 'Docker inventory failed on three consecutive storage probes'
                paths = [stdout_path, stderr_path]
                paths.extend(activity_paths(active))
                paths.append(output / 'checkpoint.json')
                if active:
                    paths.extend((active.parent / 'skill-maintenance' / active.name).glob('**/*.json'))
                latest = max((p.stat().st_mtime for p in paths if p.exists()), default=time.time())
                status['idle_seconds'] = round(max(0, time.time() - latest))
                for pattern in patterns:
                    for path in output.glob(pattern):
                        stamp = (path.stat().st_mtime_ns, path.stat().st_size)
                        if seen.get(path) == stamp:
                            continue
                        data = read_json(path)
                        if data is not None:
                            seen[path] = stamp
                            problem = problem_in(path, data)
                            if strict and path.name == 'analysis-error.json':
                                problem = f'Trace2Skill analysis error: {data.get("error")}'
                            if strict and path.name == 'validation.json' and data.get('evaluation_status') == 'ERROR':
                                problem = f'Diagnostic evaluation infrastructure error: {data.get("summary")}'
                            if strict and path.name == 'final-document-validation.json' and not data.get('passed'):
                                problem = 'Final Skill validation failed'
                            if strict and path.name == 'update.json' and data.get('decision') == 'checks_failed':
                                problem = f'Skill update rejected: {data.get("reason")}'
                            if problem:
                                reason = f'{path.relative_to(output)}: {problem}'
                                break
                    if reason:
                        break
                if not reason and any(token in stderr for token in (
                        'Traceback (most recent call last):', 'Exception in thread', 'Fatal Python error', 'No space left on device')):
                    reason = 'Uncaught runtime error in stderr; see preserved stderr log'
                if strict and not reason and 'Verification phase exhausted' in stderr:
                    reason = 'Official Skill verification exhausted its repair budget'
                if not reason and any(p.stat().st_size and previous_cleanup.get(p) != p.stat().st_mtime_ns
                                      for p in output.glob(cleanup_pattern)):
                    reason = 'Container cleanup failed; see preserved cleanup-errors.log'
                progress_lines = [line for line in stdout.splitlines() if re.match(r'\[\d\d:\d\d:\d\d\]', line)]
                scoring = bool(progress_lines and 'scoring' in progress_lines[-1])
                idle_limit = SCORING_IDLE_SECONDS if scoring else IDLE_SECONDS
                if not reason and status['idle_seconds'] > idle_limit:
                    reason = f'No execution/log progress for {status["idle_seconds"]} seconds'
                code = process.poll()
                if reason:
                    break
                if code is not None:
                    state = read_json(state_file) or state
                    if code == 0 and state.get('status') == 'completed':
                        status.update(status='completed', exit_code=code, finished_at=now())
                        save(required=True)
                        return 0
                    reason = f'Experiment process exited with code {code}; workflow status={state.get("status")}'
                    break
                # Slow Docker measurements run independently so they cannot delay
                # the five-second host disk guard, even when Docker stops responding.
                if storage_future is None and (time.monotonic() >= next_storage or
                                               previous_completed != status['completed_tasks']):
                    storage_future = storage_pool.submit(storage_snapshot, output, stdout_path.read_text(encoding='utf-8', errors='replace'))
                    next_storage = time.monotonic() + STORAGE_SECONDS
                    previous_completed = status['completed_tasks']
                save()
                time.sleep(POLL_SECONDS)
    except BaseException as exc:
        reason = f'Watchdog failed: {type(exc).__name__}: {exc}'
    finally:
        storage_pool.shutdown(wait=False, cancel_futures=True)
        if reason:
            status.update(status='stopping', reason=reason)
            # Stop API work even if writing status fails (e.g. full disk).
            try:
                save()
            except Exception as exc:
                status['status_write_error'] = str(exc)
            if process is not None and process.poll() is None:
                try:
                    killed = subprocess.run(['taskkill.exe', '/PID', str(process.pid), '/T', '/F'],
                                            capture_output=True, timeout=30)
                    status['taskkill_exit'] = killed.returncode
                    process.wait(timeout=5)
                except Exception as exc:
                    status['process_stop_error'] = str(exc)
            try:
                status['containers'] = cleanup_containers(output, stdout_path.read_text(encoding='utf-8', errors='replace'))
                status['container_cleanup_failed'] = any(not item.get('removed') for item in status['containers'])
            except Exception as exc:
                status['cleanup_error'] = str(exc)
            try:
                mark_stopped(output, reason)
            except Exception as exc:
                status['metadata_error'] = str(exc)
            failed = (status.get('process_stop_error') or status.get('cleanup_error')
                      or status.get('container_cleanup_failed'))
            status.update(status='stop_failed' if failed else 'stopped', stopped_at=now())
            try:
                save(required=True)
            except Exception as exc:
                status['status_write_error'] = str(exc)
            print(json.dumps(status, ensure_ascii=False), flush=True)
    return 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--resume', type=Path, help='Continue this interrupted workflow with saved settings')
    source.add_argument('--benchmark-config', type=Path,
                        help='Benchmark YAML for a new run (default: configs/benchmark/swe-chain-evo.yaml)')
    args = parser.parse_args()
    raise SystemExit(monitor(args.resume, args.benchmark_config))
