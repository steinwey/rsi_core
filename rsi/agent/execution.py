"""Independent mini-SWE-agent solvers; learning happens only after grading."""
import copy
import json
import time
import yaml
from rsi import UPSTREAM
from rsi.benchmarks.backends import install_skill, make_backend
from rsi.core.common import digest, write_json
from rsi.skills.directory import directory_schema, immutable, read_skill_directory
from rsi.core.model import Model, ModelFactory, RequestDeadlineExceeded, model_usage
from rsi.agent.evidence import RecordingEnvironment
from rsi.skills.store import SKILL_ROOT, skill_files
from rsi.agent.action_protocol import GuardedAgent, validate_action_protocol, REPAIR_INSTRUCTION
from rsi.core.responses import generate_object
def solver_step_limit_note(limit):
    if limit == 0:
        return ('There is no fixed step or model-call count limit in the solving phase. '
                'Wall-time, cost and action-protocol limits still apply. Finish and submit within those limits.')
    return (f"In the solving phase, you have a hard limit of {limit} combined solver calls and action-format repair attempts. "
            "Finish and submit before the limit; execution stops when it is reached. There is no automatic refinement phase.")

def progress(message):
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)

def skill_prompt(skill):
    scripts = sorted(p for p in skill_files(skill) if p.startswith("scripts/"))
    tools = "\n".join(f"- python {SKILL_ROOT}/{p} --path <repository>  (see --help)" for p in scripts)
    return (f"## Skill toolkit (installed at {SKILL_ROOT})\n"
            "Use these tools when the guide below says they apply. "
            "They inspect the repository and run tests without changing your files.\n"
            f"Read linked references on demand under {SKILL_ROOT}/references/; relative links resolve from the toolkit root.\n"
            f"Available scripts:\n{tools}\n\n" + (skill / "SKILL.md").read_text(encoding="utf-8").strip())

def execute_one(task, index, folder, skill, config, backend_factory=make_backend,
                model_factory: ModelFactory = Model):
    from minisweagent.agents.default import LimitsExceeded
    validate_action_protocol(config)
    folder.mkdir(parents=True, exist_ok=False)
    start = time.monotonic()
    backend = agent = model = env = action_formatter = None
    result = {"rollout": index, "status": "error", "messages": [], "artifact": "", "error": None,
              "skill_version": skill.name, "skill_injected": False,
              "tools_hash": digest(immutable(read_skill_directory(skill, directory_schema=directory_schema(config))))}
    try:
        backend = backend_factory(task, folder, config)
        install_skill(backend.env, skill, directory_schema=directory_schema(config))
        model = model_factory(config["model"])
        model.stream_log = folder / 'model-stream.jsonl'
        settings = yaml.safe_load((UPSTREAM / "third_party/src/minisweagent/config/extra/swebench.yaml").read_text(encoding="utf-8"))["agent"]
        settings.update(step_limit=config["step_limit"], cost_limit=config["cost_limit"])
        # The skill text is passed as a template variable so its contents are never parsed as Jinja.
        settings["system_template"] = (settings["system_template"].rstrip() + "\n\n"
                                       + solver_step_limit_note(config["step_limit"]) + "\n\n{{ rsi_skill }}")
        if task["benchmark"] == "terminal":
            settings["instance_template"] = ("Task: {{task}}\nWork in the current terminal environment. "
                "When finished, run: echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT")
        deadline = time.monotonic() + min(config["rollout_timeout"], task.get("timeout", config["rollout_timeout"]))
        model.deadline = deadline
        def repair_action(data):
            nonlocal action_formatter
            if time.monotonic() >= deadline:
                raise RequestDeadlineExceeded('No time left for action-format repair')
            if agent.budget_calls > agent.config.step_limit > 0:
                raise RequestDeadlineExceeded('No action budget left for format repair')
            if action_formatter is None:
                action_formatter = model_factory(config['judge_model'] | {'response_format': {'type': 'json_object'}})
                action_formatter.deadline = deadline
            reply = generate_object(action_formatter, REPAIR_INSTRUCTION, data,
                                    folder / 'action-repair', f'call-{model.n_calls}')
            return reply, getattr(action_formatter, 'last_finish_reason', None)
        class TimedAgent(GuardedAgent):
            def step(self):
                if time.monotonic() >= deadline:
                    raise LimitsExceeded("Rollout wall-time budget exhausted")
                if 0 < self.config.step_limit <= self.budget_calls:
                    raise LimitsExceeded('Combined solver/action-repair step budget exhausted')
                if config["cost_limit"] > 0 and model.cost + (action_formatter.cost if action_formatter else 0) >= config["cost_limit"]:
                    raise LimitsExceeded("Combined solver/action-repair cost budget exhausted")
                try:
                    return super().step()
                except RequestDeadlineExceeded as exc:
                    raise LimitsExceeded(str(exc)) from exc
        def message_index():
            assert agent is not None
            return len(agent.messages) - 1
        env = RecordingEnvironment(backend.env, folder, message_index)
        agent = TimedAgent(model, env, action_format_limit=config.get('action_format_max_consecutive', 3),
                           action_repair=repair_action if config.get('action_format_repair_enabled', False) else None,
                           action_log=folder / 'action-protocol.jsonl', **settings)
        guide = skill_prompt(skill)
        result["status"], result["submission"] = agent.run(task["prompt"], rsi_skill=guide)
        result["artifact"] = backend.artifact()
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        result["messages"] = copy.deepcopy(agent.messages) if agent else []
        result["usage"] = model_usage(model)
        result['action_repair_usage'] = model_usage(action_formatter)
        result['action_protocol'] = {'total_failures': agent.format_total if agent else 0,
                                    'consecutive_failures': agent.format_consecutive if agent else 0,
                                    'max_consecutive': config.get('action_format_max_consecutive', 3)}
        result["seconds"] = time.monotonic() - start
        result["skill_injected"] = any(m["role"] == "system" and "## Skill toolkit" in m.get("content", "")
                                       for m in result["messages"])
        result["skill_script_calls"] = copy.deepcopy(env.script_calls) if env else []
        result["skill_script_called"] = bool(result["skill_script_calls"])
        result['budget_calls'] = agent.budget_calls if agent else 0
        result['repair_attempts'] = agent.repair_calls if agent else 0
        result['executed_commands'] = env.count if env else 0
        steps = sum(m["role"] == "assistant" for m in result["messages"])
        progress(f"  {task['id']} rollout-{index} finished: {result['status']}, "
                 f"{steps} steps, {result['seconds']:.0f}s, skill injected={result['skill_injected']}, "
                 f"script command attempts={len(result['skill_script_calls'])}"
                 + (f", error: {result['error'][:200]}" if result["error"] else ""))
        try:
            write_json(folder / "trajectory.json", result)
        except BaseException:
            if backend:
                backend.close()
            raise
    return result, backend
