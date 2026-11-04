"""Complete pinned Trace2Skill framework behind the common run interface."""


def initialize(config):
    from rsi.skills.trace2skill.upstream import load
    load()


def prepare_task(task, skill, config, folder):
    return task


def evolve_skill(*args, **kwargs):
    from rsi.skills.trace2skill import evolve_skill as evolve
    return evolve(*args, **kwargs)
