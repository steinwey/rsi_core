"""Task manifests, SWE-bench compatibility and benchmark environment checks."""
from pathlib import Path

from rsi.core.common import read_json, safe_id


EVALUATION_FIELDS = {
    "instance_id", "image", "repo", "version", "FAIL_TO_PASS", "PASS_TO_PASS",
    "log_parser", "eval_type", "eval_script",
}


def evaluation_dataset_name(name: str) -> str:
    # The legacy namespace still serves rows without the v5 evaluation metadata.
    legacy = {
        f"princeton-nlp/{dataset}": f"SWE-bench/{dataset}"
        for dataset in ("SWE-bench", "SWE-bench_Lite", "SWE-bench_Verified")
    }
    return legacy.get(name, name)


def load_evaluation_rows(config, instance_ids):
    from swebench.harness.utils import load_swebench_dataset
    return load_swebench_dataset(
        evaluation_dataset_name(config["swe_dataset"]), config["swe_split"], instance_ids)


def validate_swe_tasks(tasks, config):
    """Fail before solver/API work; keep grading-only fields out of task manifests."""
    selected = [task for task in tasks if task["benchmark"] == "swe"]
    if not selected:
        return
    rows = load_evaluation_rows(config, sorted({task["id"] for task in selected}))
    by_id = {}
    for row in rows:
        missing = EVALUATION_FIELDS - row.keys()
        if missing:
            raise ValueError(
                f"SWE-bench dataset {config['swe_dataset']!r} is incompatible with the pinned "
                f"harness: missing evaluation fields {', '.join(sorted(missing))}. "
                "Use the current SWE-bench/SWE-bench_Verified dataset or a local dataset "
                "with complete evaluation metadata.")
        by_id[row["instance_id"]] = row
    for task in selected:
        row = by_id.get(task["id"])
        if row is None:
            raise ValueError(f"SWE-bench evaluation dataset has no instance {task['id']!r}")
        for field in ("repo", "base_commit", "version"):
            expected = task["instance"].get(field)
            if expected is not None and row.get(field) != expected:
                raise ValueError(
                    f"SWE-bench instance {task['id']!r}: {field} differs between the task "
                    "manifest and evaluation dataset; regenerate the manifest with prepare-swe.")


def load_tasks(path, config):
    tasks = read_json(path)
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("Task manifest must contain a nonempty list")
    for task in tasks:
        if not isinstance(task, dict) or not isinstance(task.get("id"), str):
            raise ValueError("Every task must have a string id")
        safe_id(task["id"])
        if task["benchmark"] in ("swe", "swe-evo"):
            if task["instance"]["instance_id"] != task["id"]:
                raise ValueError("SWE task ID does not match instance")
            task["prompt"] = task["instance"]["problem_statement"]
        elif task["benchmark"] == "terminal":
            from harbor.models.task.task import Task
            location = Path(task["path"])
            if not location.is_absolute():
                location = Path(path).resolve().parent / location
            task["path"] = str(location.resolve())
            parsed = Task(location)
            task["prompt"] = parsed.instruction
            task["timeout"] = parsed.config.agent.timeout_sec or config["rollout_timeout"]
        else:
            raise ValueError("benchmark must be swe, swe-evo or terminal")
    return tasks


def validate_environments(tasks, config, *, require_images=True):
    from rsi.benchmarks.evo_swe import DATASET, validate_tasks as validate_evo_tasks
    evo = [t for t in tasks if t["benchmark"] == "swe-evo"]
    swe = [t for t in tasks if t["benchmark"] == "swe"]
    if evo:
        if len(evo) == len(tasks):
            config["swe_dataset"] = DATASET
        validate_evo_tasks(evo, config, require_images=require_images)
    if swe:
        validate_swe_tasks(swe, config)
