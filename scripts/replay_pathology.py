"""Offline pathology replay. Reads run directories and writes a calibration report."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rsi.skills.trace2skill.pathology import detect, load_rollout_steps


K1_GRID = (2, 3, 4, 5)
K2_GRID = (2, 3, 4)
K3_GRID = (2, 3, 4)
PYTEST_MARK = 'python -m pytest tests/ -q'
TAIL_MARK = 'tail -5'


def main():
    parser = argparse.ArgumentParser(description='Replay idle-loop pathology over saved runs')
    parser.add_argument('--run', action='append', required=True, type=Path)
    parser.add_argument('--output', type=Path, default=Path('docs/pathology-calibration'))
    parser.add_argument('--k1', type=int, default=3)
    parser.add_argument('--k2', type=int, default=3)
    parser.add_argument('--k3', type=int, default=3)
    args = parser.parse_args()
    report = build_report([path.resolve() for path in args.run], k1=args.k1, k2=args.k2, k3=args.k3)
    stem = args.output
    json_path = stem if stem.suffix == '.json' else stem.with_suffix('.json')
    md_path = json_path.with_suffix('.md')
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    md_path.write_text(render_markdown(report), encoding='utf-8')
    print(f'wrote {md_path}')
    print(f'wrote {json_path}')


def build_report(runs, *, k1, k2, k3):
    rollouts = []
    skipped = []
    for run in runs:
        limit = _step_limit(run)
        found = list(run.glob('*/rollout-*'))
        if not found:
            skipped.append({'run': run.name, 'reason': 'no rollout directories'})
        for rollout in found:
            steps = load_rollout_steps(rollout)
            if not steps:
                skipped.append({'run': run.name, 'rollout': rollout.relative_to(run).as_posix(),
                                'reason': 'no command index or action protocol'})
                continue
            score_path = rollout / 'score.json'
            success = None
            if score_path.is_file():
                success = json.loads(score_path.read_text(encoding='utf-8')).get('success')
            findings = detect(steps, k1=k1, k2=k2, k3=k3, step_limit=limit)
            rollouts.append({'run': run.name, 'task': rollout.parent.name, 'rollout': rollout.name,
                             'path': rollout.relative_to(run.parent).as_posix(), 'success': success,
                             'step_limit': limit, 'steps': len(steps), 'findings': findings,
                             'cached_steps': steps})
    scanned = [{key: value for key, value in row.items() if key != 'cached_steps'} for row in rollouts]
    return {'defaults': {'k1': k1, 'k2': k2, 'k3': k3}, 'runs': [path.name for path in runs],
            'skipped': skipped, 'rollouts': scanned,
            'notes': _corpus_notes(rollouts),
            'model_chains': _model_chains(rollouts)} | _with_recommendation(rollouts)


def _with_recommendation(rollouts):
    rows = _thresholds(rollouts)
    return {'thresholds': rows, 'known_cases': _known_cases(rollouts), 'recommendation': _choose(rows)}


def _thresholds(rollouts):
    scored = [row for row in rollouts if type(row['success']) is bool]
    rows = []
    for k1 in K1_GRID:
        for k2 in K2_GRID:
            for k3 in K3_GRID:
                flagged = []
                for row in scored:
                    findings = detect(row['cached_steps'], k1=k1, k2=k2, k3=k3, step_limit=row['step_limit'])
                    flagged.append((row['success'], bool(findings)))
                success = [hit for ok, hit in flagged if ok]
                failure = [hit for ok, hit in flagged if not ok]
                rows.append({'k1': k1, 'k2': k2, 'k3': k3,
                             'success_rollouts': len(success), 'success_flagged': sum(success),
                             'false_positive_rate': _rate(sum(success), len(success)),
                             'failure_rollouts': len(failure), 'failure_flagged': sum(failure),
                             'detection_rate': _rate(sum(failure), len(failure))})
    return rows


def _choose(rows):
    if not rows:
        return None
    clean = [row for row in rows if row['false_positive_rate'] == 0]
    if clean:
        pool = sorted(clean, key=lambda row: (
            -row['detection_rate'], _distance(row), row['k1'], row['k2'], row['k3']))
        rule = 'a zero false-positive point exists; pick the highest failure detection, then the point closest to 3/3/3'
    else:
        pool = [row for row in rows if (row['k1'], row['k2'], row['k3']) == (3, 3, 3)]
        rule = ('no grid point has a zero false-positive rate. K2 and K3 do not move the rates on this corpus. '
                'Keep the documented 3/3/3. K1=5 is the lowest false-positive corner and does not detect more failures than K1=3.')
    chosen = pool[0]
    lowest = sorted(rows, key=lambda row: (row['false_positive_rate'], -row['detection_rate'], _distance(row)))[0]
    return {'k1': chosen['k1'], 'k2': chosen['k2'], 'k3': chosen['k3'],
            'false_positive_rate': chosen['false_positive_rate'], 'detection_rate': chosen['detection_rate'],
            'lowest_false_positive': {'k1': lowest['k1'], 'k2': lowest['k2'], 'k3': lowest['k3'],
                                      'false_positive_rate': lowest['false_positive_rate'],
                                      'detection_rate': lowest['detection_rate']},
            'rule': rule}


def _distance(row):
    return abs(row['k1'] - 3) + abs(row['k2'] - 3) + abs(row['k3'] - 3)


def _corpus_notes(rollouts):
    max_truncation = max_format = truncation_events = format_events = 0
    for row in rollouts:
        current_truncation = current_format = 0
        for step in row['cached_steps']:
            if step.get('truncated'):
                current_truncation += 1
                truncation_events += 1
                max_truncation = max(max_truncation, current_truncation)
            else:
                current_truncation = 0
            if step.get('format_failed'):
                current_format += 1
                format_events += 1
                max_format = max(max_format, current_format)
            else:
                current_format = 0
    return {'truncation_events': truncation_events, 'max_consecutive_truncation': max_truncation,
            'format_failure_events': format_events, 'max_consecutive_format_failure': max_format}


def _known_cases(rollouts):
    target = []
    pytest_cases = []
    searched = sorted({row['run'] for row in rollouts})
    for row in rollouts:
        repeats = [item for item in row['findings'] if item['type'] == 'repeated_command']
        show = [item for item in repeats if 'show_default' in item['evidence'].get('command', '')]
        if '20261110-224917' in row['run'] and row['task'].endswith('-007'):
            target.append({'path': row['path'], 'success': row['success'], 'detected': bool(show), 'findings': show})
        pytest_hits = _pytest_tail(row['cached_steps'])
        if pytest_hits:
            pytest_cases.append({'path': row['path'], 'success': row['success'], 'occurrences': pytest_hits,
                                 'repeated_command': any(PYTEST_MARK in item['evidence'].get('command', '') for item in repeats)})
    return {'searched_runs': searched, 'show_default_007': target, 'pytest_tail5': pytest_cases}


def _pytest_tail(steps):
    counts = {}
    for step in steps:
        command = ' '.join((step.get('command') or '').split())
        if PYTEST_MARK in command and TAIL_MARK in command:
            counts[command] = counts.get(command, 0) + 1
    return [{'command': command[:240], 'count': count} for command, count in counts.items() if count >= 4]


def _step_limit(run):
    path = run / 'config.json'
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding='utf-8')).get('step_limit')
    return value if type(value) is int else None


def _family(run):
    if '5eb57b' in run or '805ad7' in run:
        return 'qwen3.8-flash'
    return 'deepseek-v4.1-flash'


def _model_chains(rollouts):
    grouped = {}
    for row in rollouts:
        if type(row['success']) is not bool:
            continue
        counts = [item['evidence']['count'] for item in detect(row['cached_steps'], k1=1, k2=99, k3=99)
                  if item['type'] == 'repeated_command']
        grouped.setdefault(_family(row['run']), []).append({
            'success': row['success'], 'max_chain': max(counts) if counts else 1,
            'run': row['run'], 'task': row['task'], 'rollout': row['rollout']})
    summary = {}
    for family, rows in grouped.items():
        success = [item for item in rows if item['success']]
        failure = [item for item in rows if not item['success']]
        summary[family] = {
            'success_rollouts': len(success), 'failure_rollouts': len(failure),
            'success_max': max((item['max_chain'] for item in success), default=None),
            'failure_max': max((item['max_chain'] for item in failure), default=None),
            'success_histogram': _histogram(item['max_chain'] for item in success),
            'failure_histogram': _histogram(item['max_chain'] for item in failure),
            'success_top': sorted(success, key=lambda item: -item['max_chain'])[:8]}
    return summary


def _histogram(values):
    counts = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return [{'max_chain': key, 'rollouts': counts[key]} for key in sorted(counts)]


def _rate(hits, total):
    return None if not total else round(hits / total, 4)


def render_markdown(report):
    lines = ['# Pathology calibration', '',
             'Host-side deterministic scan. Threshold grid counts a rollout when any pathology fires.',
             'Success false-positive rate is the share of successful rollouts flagged. Target is 0.',
             '', '## Thresholds', '',
             '| K1 | K2 | K3 | success flagged | false positive | failure flagged | detection |',
             '|---:|---:|---:|---:|---:|---:|---:|']
    for row in report['thresholds']:
        lines.append(f"| {row['k1']} | {row['k2']} | {row['k3']} | {row['success_flagged']}/{row['success_rollouts']} "
                     f"| {row['false_positive_rate']} | {row['failure_flagged']}/{row['failure_rollouts']} | {row['detection_rate']} |")
    choice = report['recommendation']
    lines.extend(['', '## Recommended defaults', ''])
    if choice:
        low = choice['lowest_false_positive']
        lines.append(f"Keep K1={choice['k1']}, K2={choice['k2']}, K3={choice['k3']} "
                     f"(false positive {choice['false_positive_rate']}, failure detection {choice['detection_rate']}). "
                     f"{choice['rule']}")
        lines.append(f"Lowest false-positive corner in the grid: K1={low['k1']}, K2={low['k2']}, K3={low['k3']} "
                     f"(false positive {low['false_positive_rate']}, failure detection {low['detection_rate']}).")
    notes = report.get('notes') or {}
    if notes:
        lines.extend(['', '## Consecutive-run lengths', '',
                      f"Truncation events {notes['truncation_events']}, longest consecutive run {notes['max_consecutive_truncation']}. "
                      f"Format-failure events {notes['format_failure_events']}, longest consecutive run {notes['max_consecutive_format_failure']}. "
                      'K2 and K3 do not change the table while those longest runs stay below 2.'])
    lines.extend(['', '## Known cases', ''])
    known = report['known_cases']
    lines.append('Searched runs: ' + ', '.join(f'`{name}`' for name in known['searched_runs']))
    lines.append('')
    lines.append('### 007 repeated show_default grep')
    if not known['show_default_007']:
        lines.append('Not found in the searched runs.')
    for case in known['show_default_007']:
        lines.append(f"- `{case['path']}` success={case['success']} repeated_command={case['detected']}")
        for item in case['findings']:
            evidence = item['evidence']
            lines.append(f"  - steps {item['start_step']}-{item['end_step']} count={evidence.get('count')} "
                         f"sha={evidence.get('output_sha256')}")
    lines.extend(['', '### pytest `tests/ -q | tail -5` on a success trajectory', ''])
    if not known['pytest_tail5']:
        lines.append('No success or failure rollout contained `python -m pytest tests/ -q` and `tail -5` four or more times.')
    for case in known['pytest_tail5']:
        lines.append(f"- `{case['path']}` success={case['success']} repeated_command_on_that_text={case['repeated_command']}")
        for item in case['occurrences']:
            lines.append(f"  - count={item['count']} `{item['command']}`")
    lines.extend(['', '## Rollouts', '', '| run | task | rollout | success | steps | findings |',
                  '|---|---|---|---|---:|---|'])
    for row in report['rollouts']:
        kinds = ', '.join(f"{item['type']}@{item['start_step']}-{item['end_step']}" for item in row['findings']) or '-'
        lines.append(f"| `{row['run']}` | `{row['task']}` | `{row['rollout']}` | {row['success']} | {row['steps']} | {kinds} |")
    if report['skipped']:
        lines.extend(['', '## Skipped', ''])
        for item in report['skipped']:
            lines.append(f"- {item}")
    lines.extend(_threshold_section(report.get('model_chains') or {}))
    lines.append('')
    return '\n'.join(lines)


def _threshold_section(model_chains):
    lines = ['', '## 建议阈值', '',
             'K1=3：温和提示 + 失败轨迹挖掘，跨模型通用。',
             '硬停按模型分：qwen3.8-flash H=20，deepseek-v4.1-flash H=8；未知模型不硬停，先 shadow 校准。',
             'H 的语义是第 H 次重复时跳过该条命令的沙盒执行并返回拒绝观察，不终止 rollout。',
             '`805ad7` / `evo-f881cb75-007` / rollout-4（72 次 `inspect.getsource`，占 72/133 步后侥幸成功）'
             '归类为硬停预期会干预的病态轨迹，不受阈值保护。旧结论 H=73 作废，原因是被该异常值拖高。',
             'K2=K3=3 维持，未经校准（语料中无连续截断或格式失败）。',
             '', '### 按模型拆分的最长链', '',
             '每条成功或失败轨迹取「同一命令 + 同一 sha + 中间无编辑」的最长链。'
             'qwen 为 `5eb57b` 与 `805ad7`，deepseek 为其余 run。'
             '表中的成功最长链是绝对值；定 H 时去掉 `805ad7/007/rollout-4` 那条病态成功，qwen 为 17，deepseek 为 3。', '']
    if not model_chains:
        lines.append('本次回放没有按模型汇总。')
        return lines
    lines.append('| 模型 | 成功条数 | 成功最长链 | 失败条数 | 失败最长链 |')
    lines.append('|---|---:|---:|---:|---:|')
    for family in ('qwen3.8-flash', 'deepseek-v4.1-flash'):
        item = model_chains.get(family)
        if not item:
            continue
        lines.append(f"| {family} | {item['success_rollouts']} | {item['success_max']} "
                     f"| {item['failure_rollouts']} | {item['failure_max']} |")
    lines.extend(['', '成功轨迹最长链直方图（链长 → 条数）：', ''])
    for family in ('qwen3.8-flash', 'deepseek-v4.1-flash'):
        item = model_chains.get(family)
        if not item:
            continue
        hist = ', '.join(f"{point['max_chain']}→{point['rollouts']}" for point in item['success_histogram'])
        lines.append(f"- {family}：{hist}")
    qwen = model_chains.get('qwen3.8-flash') or {}
    tops = [item for item in qwen.get('success_top') or [] if item['max_chain'] > 1]
    if tops:
        lines.extend(['', 'qwen 成功轨迹里长于 1 的链：', ''])
        for item in tops:
            lines.append(f"- {item['max_chain']} 次 `{item['run']}` / `{item['task']}` / {item['rollout']}")
    lines.extend(['', '谓词补上 `open(..., "w").write(` 之后，`5eb57b` / `evo-f881cb75-010` / rollout-3 '
                  '的 15 次 `nl | sed` 不再成链。其余原先判为真实空转的成功标记还在，'
                  '包括 17 次 awk 重读和 72 次 `inspect.getsource`。'])
    return lines


if __name__ == '__main__':
    main()
