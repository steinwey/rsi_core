"""Preserve the final report around ReAct completion control lines."""
import re


def extract_report(outcome):
    """Use only the final assistant response, never earlier steps or tool output.

    Upstream final_answer stops at TASK_COMPLETE. Some compatible models put
    the report after that marker, so recover the final response's full text.
    """
    text = outcome.final_answer
    steps = outcome.steps
    source = 'final_answer'
    if steps and steps[-1].is_final and isinstance(steps[-1].thought, str):
        text = steps[-1].thought
        source = 'final_assistant_response'
    report, removed = re.subn(r'(?m)^\s*ACTION:\s*TASK_COMPLETE\s*$', '', text)
    report = report.strip()
    return report, {'source': source, 'raw_chars': len(text),
                    'upstream_answer_chars': len(outcome.final_answer),
                    'saved_chars': len(report), 'completion_lines_removed': removed}
