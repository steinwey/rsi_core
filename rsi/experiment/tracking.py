"""Local experiment totals, including Trace2Skill usage."""


def task_complete(report):
    if report.get('phase') == 'train':
        return (report.get('success') is None and report.get('selected') is None
                and report.get('supervision_complete') is True
                and sorted(report.get('graded_rollouts', [])) in ([0], list(range(5))))
    return report.get('phase') == 'test' and type(report.get('success')) is bool


def summary_metrics(reports):
    scored = [r for r in reports if r.get('success') is not None]
    resolved = sum(bool(r['success']) for r in scored)
    totals = {key: sum(r.get(key, 0) for r in reports)
              for key in ('input_tokens', 'output_tokens', 'seconds', 'group_seconds',
                          'skill_update_seconds')}
    totals.update(tasks=len(reports), scored=len(scored))
    evaluated = [r for r in reports if r.get('phase') != 'train']
    if evaluated:
        totals.update(resolved=resolved, accuracy=resolved / len(evaluated),
                      scoring_coverage=len(scored) / len(evaluated))
    totals['training_tasks'] = sum(r.get('phase') == 'train' for r in reports)
    totals['supervised_routes'] = sum(len(r.get('graded_rollouts', [])) for r in reports if r.get('phase') == 'train')
    totals['tokens'] = totals['input_tokens'] + totals['output_tokens']
    return totals
