"""Five-route training and single-route frozen evaluation."""

MODES = {'parallel': 5, 'single': 1}


def validate_execution_mode(config):
    mode = config.get('execution_mode', 'parallel')
    if mode not in MODES or type(config.get('rollouts')) is not int or config['rollouts'] != MODES[mode]:
        raise ValueError('Use five parallel rollouts or one single rollout')
    if config.get('evolve', True) and mode != 'parallel':
        raise ValueError('Training requires exactly five parallel rollouts')
    workers = config.get('rollout_workers', config['rollouts'])
    if type(workers) is not int or workers < 1:
        raise ValueError('rollout_workers must be a positive integer')
    return mode


def set_execution_mode(config, mode):
    if mode not in MODES:
        raise ValueError('Unknown execution mode')
    config.update(execution_mode=mode, rollouts=MODES[mode])
    if 'rollout_workers' in config:
        config['rollout_workers'] = min(config['rollout_workers'], MODES[mode])
    validate_execution_mode(config)
    return config


def set_rollout_execution(config, rollouts, parallel):
    if type(rollouts) is not int or type(parallel) is not bool or (rollouts, parallel) not in ((5, True), (1, False)):
        raise ValueError('Use five parallel rollouts or one single rollout')
    return set_execution_mode(config, 'parallel' if parallel else 'single')
