"""Whole-file budgets with explicit strict/estimated counter selection."""
from dataclasses import dataclass
from typing import Protocol

from rsi.skills.index import sha
from rsi.skills.store import SKILL_ROOT


class BudgetError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CounterInfo:
    model: str
    identity: str
    accuracy: str
    statement: str


class TokenCounter(Protocol):
    info: CounterInfo

    def count(self, text: str) -> int: ...


# Explicit capability decisions, not aliases to an unrelated vocabulary. See P2 docs.
UNSUPPORTED_MODELS = {
    'gpt-6-luna': 'No verified offline tokenizer mapping for gpt-6-luna; remote counting is disabled.',
    'qwen3.8-max': 'No verified exact offline tokenizer mapping for qwen3.8-max.',
    'qwen3.8-flash': 'No verified exact offline tokenizer mapping for qwen3.8-flash.',
}


def token_counter(model_config, *, mode="strict", estimated=None):
    if mode == "estimated":
        from rsi.skills.tokenizers import EstimatedCounter
        return EstimatedCounter(model_config, estimated)
    if mode != "strict" or estimated is not None:
        raise BudgetError("counter_unavailable", "Invalid counter mode or strict/estimated configuration mix.")
    name = model_config.get('name')
    if name == 'deepseek-v4.1-flash':
        from rsi.skills.tokenizers import DeepSeekCounter
        return DeepSeekCounter(model_config)
    raise BudgetError('counter_unavailable', UNSUPPORTED_MODELS.get(name,
        f'No verified strict TokenCounter registered for {name!r}.'))


def check_counter(counter, model, *, mode="strict"):
    allowed = ("exact_text", "test_only") if mode == "strict" else ("estimated",) if mode == "estimated" else ()
    info = getattr(counter, 'info', None)
    if (not isinstance(info, CounterInfo) or info.model != model or not info.identity
            or not info.statement or info.accuracy not in allowed):
        raise BudgetError('counter_unavailable', 'Counter must identify the exact solver model and accuracy.')
    # test_only can only arrive through explicit Python dependency injection. No config switch
    # enables it, and the default factory never returns a synthetic counter.
    return info


def count_text(counter, text):
    try:
        value = counter.count(text)
    except Exception as exc:
        raise BudgetError('counter_error', f'TokenCounter failed: {type(exc).__name__}') from exc
    if type(value) is not int or value < 0 or (text and value == 0):
        raise BudgetError('counter_error', 'TokenCounter returned an invalid count.')
    return value


def render_context(files, selected=(), *, access=None):
    scripts = sorted(p for p in files if p.startswith('scripts/'))
    tools = '\n'.join(f'- python {SKILL_ROOT}/{p} --path <repository>  (see --help)' for p in scripts)
    loaded = '\n'.join(f'- {p} (complete file)' for p in selected) or '- None (core only)'
    text = (f'## Skill toolkit (installed at {SKILL_ROOT})\n'
            'Use these tools when the guide below says they apply. '
            'They inspect the repository and run tests without changing your files.\n'
            f'Available scripts:\n{tools}\n\n'
            f'The complete Skill directory remains available through Bash at {SKILL_ROOT}. '
            f'Read other references under {SKILL_ROOT}/references/ on demand; '
            f'{SKILL_ROOT}/routing/index.yaml lists all references. '
            'Root guide links resolve from the toolkit root; reference links resolve from their file directory.\n\n'
            f'Automatically loaded references:\n{loaded}\n\n'
            '## Core guide (SKILL.md)\n' + files['SKILL.md'])
    if access and access['enabled']:
        from rsi.skills.access import guidance
        if not access['expose_index']:
            text = text.replace(f'{SKILL_ROOT}/routing/index.yaml lists all references. ', '')
        text += guidance(access)
    for path in selected:
        text += f'\n\n---\n## Reference: {path} (complete file)\n' + files[path]
    return text


@dataclass(frozen=True)
class SkillContext:
    text: str
    context_hash: str
    initial_skill_tokens: int
    selected_references: tuple[str, ...]
    skill_version: str = ''
    index_hash: str = ''
    route_id: str = ''


@dataclass(frozen=True)
class BudgetSelection:
    context: SkillContext
    core_tokens: int
    excluded: tuple[tuple[str, str, int | None], ...]


def select_context(files, ranked_paths, counter, *, budget_limit, top_k, access=None):
    core = render_context(files, access=access)
    core_tokens = count_text(counter, core)
    if core_tokens > budget_limit:
        raise BudgetError('core_over_budget', f'Core SkillContext uses {core_tokens} tokens; limit {budget_limit}.')
    selected, excluded = [], []
    for path in ranked_paths:
        if len(selected) == top_k:
            excluded.append((path, 'top_k_limit', None))
            continue
        trial = render_context(files, (*selected, path), access=access)
        trial_tokens = count_text(counter, trial)
        if trial_tokens <= budget_limit:
            selected.append(path)
        else:
            excluded.append((path, 'reference_over_budget', trial_tokens))
    text = render_context(files, selected, access=access)
    final_tokens = count_text(counter, text)
    if final_tokens > budget_limit:
        raise BudgetError('final_over_budget', 'Final complete SkillContext exceeds the budget.')
    return BudgetSelection(SkillContext(text, sha(text), final_tokens, tuple(selected)),
                           core_tokens, tuple(excluded))
