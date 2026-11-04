"""Whole learning frameworks, selected once per run and saved in its config."""
from importlib import import_module

FRAMEWORKS = ('trace2skill', 'reasoningbank')


def get_framework(config):
    name = config.get('skill_update_method', 'trace2skill')
    if name not in FRAMEWORKS:
        raise ValueError(f'Unknown framework: {name}')
    return import_module(f'rsi.frameworks.{name}')


def evolve_skill(*args, **kwargs):
    return get_framework(args[3]).evolve_skill(*args, **kwargs)
