"""Upstream report prompts with only the spreadsheet environment replaced by SWE."""
from rsi.skills.trace2skill.upstream import SOURCE


def analysis_prompt(success):
    folder = SOURCE / 'analysis'
    if success:
        return (folder / 'success_analysis_system_llm.txt').read_text(encoding='utf-8').replace(
            'spreadsheet manipulation tasks', 'software repository tasks')
    original = (folder / 'error_analysis_system.txt').read_text(encoding='utf-8')
    # Keep the official report-writing instructions and item format. The original
    # spreadsheet workspace/tool instructions cannot operate on a SWE repository.
    start = original.index('Section 1:') if 'Section 1:' in original else original.index('## OUTPUT FORMAT')
    end = original.index('Your working_directory:')
    report = original[start:end]
    a = report.find('## Important Perspective Constraints')
    b = report.find('## OUTPUT FORMAT')
    if 0 <= a < b:
        report = report[:a] + report[b:]
    return ('Analyze the supplied failed software-repository trajectory and formal test feedback. '
            'Use the supplied read_evidence, read_source and run_probe tools to investigate. '
            'Use run_probe to apply a minimal diagnostic fix in the disposable repository, then call '
            'evaluate_diagnostic_patch for formal SWE-bench evaluation. A local command exit code or '
            'a PASS claim in prose is not formal verification. If evaluation fails, use its feedback '
            'to continue investigating within the turn budget. Changes after PASS require re-evaluation. '
            'No reference patch or spreadsheet artifacts are available. Produce the official '
            'Failure Cause Items and Failure Memory Items report below. Write the complete report '
            'before a standalone ACTION: TASK_COMPLETE line; do not replace the report with the signal.\n' + report)
