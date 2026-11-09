"""Build post-scoring Skill feedback from grader observations, never reference patches."""
import hashlib
import re
from pathlib import Path

from rsi.core.common import read_json

MAX_TEST_NAMES = 100
MAX_FAILURE_CHARS = 24000


def failure_excerpt(path):
    """Keep pytest errors and nearby context; make every omission explicit."""
    if not path.exists():
        return {'available': False, 'text': '', 'reason': 'Grader test output was not saved'}
    raw = path.read_bytes()
    lines = raw.decode('utf-8', errors='replace').splitlines(keepends=True)
    selected = set()
    for index, line in enumerate(lines):
        if re.match(r'^(?:E\s|E$|FAILED\s|ERROR\s)', line):
            selected.update(range(max(0, index - 2), min(len(lines), index + 3)))
    excerpt = ''.join(line for index, line in enumerate(lines) if index in selected)
    text = excerpt[:MAX_FAILURE_CHARS]
    return {'available': True, 'source': str(path.resolve()),
            'source_sha256': hashlib.sha256(raw).hexdigest(),
            'source_chars': sum(map(len, lines)), 'matched_chars': len(excerpt),
            'included_chars': len(text), 'omitted_chars': sum(map(len, lines)) - len(text),
            'truncated': len(excerpt) > len(text), 'text': text}


def build_supervision(task, results, selected, score):
    """Build feedback for one graded artifact; used by the all-route collector."""
    if type(score.get('success')) is not bool or score.get('infra_failure'):
        raise ValueError('Skill supervision requires a completed non-infrastructure grade')
    routes = {result['rollout']: result for result in results}
    if type(selected) is not int or selected not in routes:
        raise ValueError('Skill supervision must identify an existing selected rollout')
    grade = {'success': score['success']}
    if isinstance(score.get('reason'), str):
        grade['reason'] = score['reason']
    if isinstance(score.get('rewards'), dict):
        grade['rewards'] = {k: v for k, v in score['rewards'].items()
                            if isinstance(v, (int, float, bool))}
    feedback = {'schema': 1, 'task_id': task['id'], 'stage': 'post_scoring',
                'selected_rollout': selected,
                'artifact_sha256': hashlib.sha256(routes[selected]['artifact'].encode('utf-8')).hexdigest(),
                'routes': [{'rollout': route, 'graded': route == selected,
                            'success': score['success'] if route == selected else None}
                           for route in sorted(routes)],
                'grade': grade,
                'limitations': ['Only the selected final artifact was formally graded.',
                                'A failing grade alone does not identify a cause. Untested routes have unknown correctness.',
                                'Diagnostic excerpts may omit context; passing tests do not prove universal correctness.']}
    if score.get('report'):
        path = Path(score['report']).resolve()
        report = read_json(path)[task['id']]
        if report.get('infra_failure') or type(report.get('resolved')) is not bool or report['resolved'] != score['success']:
            raise ValueError('Grader report disagrees with the completed score')
        feedback['report_source'] = str(path)
        feedback['report_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        for key in ('patch_exists', 'patch_is_None', 'patch_successfully_applied'):
            if key in report:
                grade[key] = report[key]
        grade['tests_status'] = {}
        for group in ('FAIL_TO_PASS', 'PASS_TO_PASS', 'FAIL_TO_FAIL', 'PASS_TO_FAIL'):
            item = report.get('tests_status', {}).get(group, {})
            buckets = {}
            for outcome in ('success', 'failure'):
                names = item.get(outcome, [])
                if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
                    raise ValueError('Malformed grader test status')
                buckets[outcome] = {'count': len(names), 'tests': names[:MAX_TEST_NAMES],
                                    'omitted': max(0, len(names) - MAX_TEST_NAMES)}
            grade['tests_status'][group] = buckets
        feedback['failure_output'] = failure_excerpt(path.with_name('test_output.txt'))
    else:
        feedback['failure_output'] = {'available': False, 'text': '',
                                      'reason': 'This backend supplied an outcome without a detailed test report'}
    return feedback


def build_all_supervision(task, results, selected, scores):
    """Keep every route label; training has no selected answer or task grade."""
    ids = {result['rollout'] for result in results}
    if len(results) not in (1, 5) or len(ids) != len(results) or set(scores) != ids or (selected is not None and selected not in ids):
        raise ValueError('All-route supervision requires one or five matching route grades')
    routes = []
    for route in sorted(ids):
        item = build_supervision(task, results, route, scores[route])
        routes.append({'rollout': route, 'graded': True, 'success': item['grade']['success'],
                       **{key: item[key] for key in ('artifact_sha256', 'grade', 'failure_output',
                                                    'report_source', 'report_sha256') if key in item}})
    if selected is None:
        scope = ('All five artifacts were graded only as training supervision.' if len(results) == 5
                 else 'The single artifact was graded only as training supervision.')
        return {'schema': 2, 'task_id': task['id'], 'stage': 'post_scoring',
                'selected_rollout': None, 'accuracy_scope': 'not_applicable', 'routes': routes,
                'limitations': [scope,
                                'No answer selection or training accuracy is computed.',
                                'Passing tests do not prove general correctness; failures alone do not establish causes.']}
    picked = next(route for route in routes if route['rollout'] == selected)
    return {'schema': 2, 'task_id': task['id'], 'stage': 'post_scoring',
            'selected_rollout': selected, 'accuracy_scope': 'selected_only',
            'artifact_sha256': picked['artifact_sha256'], 'grade': picked['grade'],
            'routes': routes,
            'limitations': ['All five final artifacts were independently graded after selection.',
                            'Task accuracy uses only the selected route, not any-route success or route averages.',
                            'Compare route-specific evidence; passing tests do not prove general correctness.',
                            'Diagnostic excerpts may omit context; false outcomes alone do not establish causes.']}
