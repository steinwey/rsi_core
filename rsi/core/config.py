"""Load experiment settings and resolve credentials for the roles in use."""
import os
import re
from pathlib import Path

import yaml

from rsi import ROOT, DEPENDENCY_ROOT
from rsi.agent.rollouts import MODES, set_execution_mode, set_rollout_execution, validate_execution_mode
from rsi.core.model import BANNED_MODEL_OPTIONS
from rsi.agent.action_protocol import validate_action_protocol
from rsi.benchmarks.tasks import evaluation_dataset_name

DEFAULT_CONFIG = ROOT / "configs/agent/qwen.yaml"
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


def load_env():
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env", override=False)
    load_dotenv(DEPENDENCY_ROOT / ".env", override=False)


def load_benchmark_config(path):
    """Load a run specification; resolve its paths relative to this YAML file."""
    path = Path(path).expanduser().resolve()
    try:
        settings = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid benchmark YAML {path}: {exc}") from exc
    if not isinstance(settings, dict):
        raise ValueError("Benchmark config must be a mapping")
    paths = {"config", "tasks", "test_tasks", "initial_skill", "output", "runs_dir"}
    strings = {"execution_mode", "model", "label", "swe_dataset", "swe_split"}
    booleans = {"fixed_skill", "wandb", "parallel"}
    unknown = settings.keys() - (paths | strings | booleans | {"rollouts"})
    if unknown:
        raise ValueError("Unknown benchmark fields: " + ", ".join(sorted(map(str, unknown))))
    for key in ("config", "tasks"):
        if settings.get(key) is None:
            raise ValueError(f"Benchmark config requires {key}")
    settings = {key: value for key, value in settings.items() if value is not None}
    for key, value in settings.items():
        if key in booleans:
            if type(value) is not bool:
                raise ValueError(f"Benchmark {key} must be a boolean")
        elif key == "rollouts":
            if type(value) is not int or value <= 0:
                raise ValueError("Benchmark rollouts must be a positive integer")
        elif not isinstance(value, str) or not value.strip():
            raise ValueError(f"Benchmark {key} must be a nonempty string")
    for key in paths & settings.keys():
        location = Path(settings[key]).expanduser()
        settings[key] = str((path.parent / location).resolve())
    if "execution_mode" in settings and settings["execution_mode"] not in MODES:
        raise ValueError("Benchmark execution_mode must be one of: " + ", ".join(MODES))
    if "rollouts" in settings or "parallel" in settings:
        if "rollouts" not in settings or "parallel" not in settings:
            raise ValueError("Benchmark rollouts and parallel must be specified together")
        if "execution_mode" in settings:
            raise ValueError("Use rollouts/parallel or execution_mode, not both")
    if settings.get("test_tasks") and settings.get("fixed_skill"):
        raise ValueError("Benchmark test_tasks and fixed_skill are mutually exclusive")
    if settings.get("output") and settings.get("runs_dir"):
        raise ValueError("Benchmark output and runs_dir are mutually exclusive")
    if "swe_dataset" in settings:
        settings["swe_dataset"] = evaluation_dataset_name(settings["swe_dataset"])
    return settings


def apply_benchmark_execution(config, benchmark):
    """Apply YAML execution settings before any explicit CLI mode override."""
    if "rollouts" in benchmark:
        set_rollout_execution(config, benchmark["rollouts"], benchmark["parallel"])
    elif benchmark.get("execution_mode"):
        set_execution_mode(config, benchmark["execution_mode"])
    return config


def model_configs(config, *, active_only=False):
    blocks = {role: config[role] for role in ("model", "judge_model", "skill_error_model", "skill_success_model", "skill_editor_model")
              if isinstance(config.get(role), dict)}
    if active_only:
        trace = config.get('skill_update_method', 'trace2skill') == 'trace2skill'
        enabled = {"model": True, "judge_model": config.get("evolve", True) or config.get("rollouts", 5) > 1 or config.get('action_format_repair_enabled', False),
                   "skill_error_model": trace and config.get("evolve", True),
                   "skill_success_model": trace and config.get("evolve", True),
                   "skill_editor_model": config.get("evolve", True)}
        blocks = {role: block for role, block in blocks.items() if enabled[role]}
    return blocks


def load_config(path, *, framework=None):
    config = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(config, dict) or any(not isinstance(config.get(k), dict) for k in ("model", "judge_model")):
        raise ValueError("Config must contain model and judge_model mappings")
    if framework is not None:
        config['skill_update_method'] = framework
    unsupported = {'compression', 'skill_compression', 'updater_compression', 'skill_update', 'compression_learning', 'selector_evidence', 'planner_model',
                   'reflection_enabled', 'reflection_max_rounds', 'reflection_step_limit', 'reflection_model', 'reflection_format_model'} & config.keys()
    if unsupported or config.get('planner_enabled'):
        raise ValueError('Unsupported core extensions: ' + ', '.join(sorted(unsupported)))
    config.setdefault('action_format_max_consecutive', 3)
    validate_action_protocol(config)
    config.setdefault("image_pull_timeout", 7200)
    config["swe_dataset"] = evaluation_dataset_name(config["swe_dataset"])
    validate_execution_mode(config)
    if type(config.get('step_limit')) is not int or config['step_limit'] < 0:
        raise ValueError('step_limit must be a nonnegative integer; 0 disables the step limit')
    for key in ("command_timeout", "image_pull_timeout", "rollout_timeout", "evaluation_timeout"):
        if config[key] <= 0:
            raise ValueError(f"{key} must be positive")
    solver = config['model'].get('name')
    if solver not in ('qwen3.8-max', 'qwen3.8-flash', 'gpt-6-luna'):
        raise ValueError('model must use qwen3.8-max, qwen3.8-flash or gpt-6-luna')
    roles = ('model', 'judge_model', 'skill_error_model', 'skill_success_model', 'skill_editor_model')
    all_luna = solver == 'gpt-6-luna' and all(model_configs(config).get(role, {}).get('name') == solver for role in roles)
    for role in roles:
        expected = solver if all_luna or role == 'model' or (role == 'judge_model' and solver != 'gpt-6-luna') else 'qwen3.8-max'
        if not isinstance(config.get(role), dict) or config[role].get('name') != expected:
            raise ValueError(f'{role} must explicitly use {expected}')
    from rsi.frameworks import FRAMEWORKS
    if config.get('skill_update_method') not in FRAMEWORKS:
        raise ValueError('skill_update_method must be trace2skill or reasoningbank')
    defaults = {'analysis_max_turns': 8, 'timeout': 1800, 'max_workers': 2, 'merge_batch_size': 5, 'max_merge_levels': 3}
    config.setdefault('trace2skill', {})
    if not isinstance(config.get('trace2skill'), dict) or set(config['trace2skill']) - defaults.keys():
        raise ValueError('Invalid trace2skill configuration')
    config['trace2skill'] = defaults | config['trace2skill']
    for key, value in config['trace2skill'].items():
        if type(value) is not int or value <= 0:
            raise ValueError(f'trace2skill.{key} must be a positive integer')
    if config['trace2skill']['merge_batch_size'] < 2:
        raise ValueError('Trace2Skill merge_batch_size must be at least 2')
    rb_defaults = {'timeout': 1800, 'max_repair_rounds': 3}
    rb = config.get('reasoningbank', {})
    if not isinstance(rb, dict) or rb.keys() - rb_defaults.keys():
        raise ValueError('Invalid reasoningbank configuration')
    config['reasoningbank'] = rb_defaults | rb
    if any(type(value) is not int or value <= 0 for value in config['reasoningbank'].values()):
        raise ValueError('ReasoningBank settings must be positive integers')
    from rsi.skills.directory import directory_schema
    directory_schema(config)
    for feature in ('skill_maintenance', 'reference_maintenance', 'skill_routing'):
        config.setdefault(feature, {'enabled': False})
    config.setdefault('skill_directory', {'schema': 0})
    if config['skill_maintenance'].get('enabled'):
        from rsi.skills.maintenance import settings
        config['skill_maintenance'] = settings(config)
    blocks = model_configs(config)
    for block in (*blocks.values(), config.get("wandb") or {}):
        if isinstance(block, dict) and any(key in block for key in ("api_key", "key")):
            raise ValueError("Put credentials in environment variables, not saved configuration")
    allowed = config.get("allowed_models")
    if allowed is not None and (not isinstance(allowed, list) or not allowed or len(set(allowed)) != len(allowed)):
        raise ValueError("allowed_models must be a non-empty list of unique model ids")
    for role, block in blocks.items():
        rate_delay = block.get('rate_limit_retry_delay')
        if rate_delay is not None and (type(rate_delay) not in (int, float) or not 0 < rate_delay < float('inf')):
            raise ValueError(f'{role}.rate_limit_retry_delay must be a positive finite number')
        banned = BANNED_MODEL_OPTIONS & block.keys()
        if banned:
            raise ValueError("Model fallback is not enabled: " + ", ".join(sorted(banned)))
        env_name = block.get("api_key_env")
        if env_name is not None and (not isinstance(env_name, str) or not _ENV_NAME.fullmatch(env_name)):
            raise ValueError("api_key_env must name an environment variable, not contain a secret")
        if allowed is not None and block.get("name") not in allowed:
            raise ValueError(f"{block.get('name')} is not in allowed_models")
    return config


def select_model(config, model_name):
    """Select solver/judge without changing the configured provider or credentials."""
    if not model_name:
        return config
    allowed = config.get("allowed_models")
    if not allowed:
        raise ValueError("This config does not declare allowed_models; change model.name in the config file")
    if model_name not in allowed:
        raise ValueError(f"{model_name} is not one of: {', '.join(allowed)}")
    if model_name == config['model']['name']:
        return config
    if config['model']['name'] == 'gpt-6-luna':
        raise ValueError('Use a Qwen config to switch providers; Luna credentials cannot be reused for Qwen')
    if model_name not in ('qwen3.8-max', 'qwen3.8-flash'):
        raise ValueError('Only qwen3.8-max and qwen3.8-flash are supported')
    for role in ('model', 'judge_model'):
        config[role]['name'] = model_name
    return config


def credential_envs(config, *, solver_only=False):
    blocks = {"model": config["model"]} if solver_only else model_configs(config, active_only=True)
    names = {block.get("api_key_env") or
             ("DEEPSEEK_API_KEY" if str(block.get("name", "")).startswith("deepseek/") else None)
             for block in blocks.values()}
    return sorted(name for name in names if name is not None)


def require_credentials(config, *, solver_only=False):
    missing = [name for name in credential_envs(config, solver_only=solver_only) if not os.getenv(name)]
    if missing:
        raise RuntimeError("Set " + ", ".join(missing) + " in the environment")
