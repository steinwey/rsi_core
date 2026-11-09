"""Deterministic Skill-management fixtures. Human labels live here, not in model output."""
import json
from pathlib import Path

FRONT = """---
name: {name}
description: >-
  Diagnose and fix SWE-bench issues in software repositories. Use when
  reproducing a reported failure and choosing a focused regression check.
---

# Test and diagnose

Use for SWE-bench software-repair tasks when identifying the test framework and planning a targeted reproduction.

1. Inspect the repository and reproduce the reported failure before changing production code.
2. Run `python /opt/rsi-skills/scripts/inspect_project.py --path <repository>`.
3. Prefer the smallest relevant test, then related regression tests.
4. Report observed failures and distinguish environment errors from product bugs.
5. Other references remain readable through Bash at `/opt/rsi-skills/references/`. The index at `/opt/rsi-skills/routing/index.yaml` lists them.

"""


def _topic(title, condition, detail, repeats):
    body = f"## {title}\n\n{condition}\n\n{detail}\n"
    filler = "Record the exception text and the edited call site. "
    body += filler * repeats
    if not body.endswith("\n"):
        body += "\n"
    return body


EXPERIENCES = {
    "python_importerror": {
        "title": "Python ImportError recovery",
        "condition": "When Python raises `ImportError` during pytest collection, preserve the package layout.",
        "detail": "Check `sys.path`, the missing module name, and whether the test runner changed the working directory. Do not rewrite unrelated imports.",
        "target": "references/python_imports.md",
    },
    "pytest_fixture_scope": {
        "title": "pytest fixture scope",
        "condition": "When a pytest fixture uses function scope, isolate mutable state from later tests.",
        "detail": "Create the resource inside the fixture and close it in teardown. Do not store that resource on a session object.",
        "target": "references/testing_strategies.md",
    },
    "pytest_asyncio": {
        "title": "async function tests",
        "condition": "When pytest runs a coroutine test, await the coroutine before asserting the result.",
        "detail": "Mark the test with pytest-asyncio and close the event loop after the assertion. Do not call the coroutine without await.",
        "target": "references/async_tests.md",
    },
    "git_apply": {
        "title": "Git patch application",
        "condition": "When git apply rejects a patch, run git apply --check before changing the source.",
        "detail": "Compare the rejected hunk with the current file. Keep the patch limited to the reported lines and rerun the focused pytest.",
        "target": "references/git_apply.md",
    },
}


def experience_section(key):
    item = EXPERIENCES[key]
    return _topic(item["title"], item["condition"], item["detail"], 2)


def write_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8", newline="\n")


def build(root):
    root = Path(root)
    scenarios = {}
    scenarios["s1_short"] = _s1(root / "s1_short")
    scenarios["s2_near_limit"] = _s2(root / "s2_near_limit")
    scenarios["s3_three_references"] = _s3(root / "s3_three_references")
    scenarios["s4_redundancy"] = _s4(root / "s4_redundancy")
    scenarios["s5_periodic"] = _s5(root / "s5_periodic")
    scenarios["s6_routing"] = _s6(root / "s6_routing")
    write_text(root / "catalog.json", json.dumps({
        "scenarios": list(scenarios),
        "note": "Labels are human expectations for the offline tests. They are not model judgments.",
    }, indent=2) + "\n")
    return scenarios


def _s1(folder):
    text = FRONT.format(name="scenario-s1")
    text += ("Keep the repair limited to the reported behavior. "
             "Read the failure output before editing production code. ") * 20
    if not text.endswith("\n"):
        text += "\n"
    write_text(folder / "SKILL.md", text)
    payload = {
        "initial_references": [],
        "character_count": len(text),
        "experiences": list(EXPERIENCES),
        "human_expectation": {
            "below_hard_limit_program_must_not_invent_a_reference": True,
            "updater_may_keep_text_in_skill_md_or_create_a_reference": True,
            "creation_is_a_model_decision_until_the_root_exceeds_10000": True,
        },
    }
    write_text(folder / "expected.json", json.dumps(payload, indent=2) + "\n")
    write_text(folder / "experiences.json", json.dumps(EXPERIENCES, indent=2) + "\n")
    return payload


def _s2(folder):
    parts = [FRONT.format(name="scenario-s2")]
    specs = [
        ("Python ImportError recovery", EXPERIENCES["python_importerror"]["condition"], EXPERIENCES["python_importerror"]["detail"], 36),
        ("pytest fixture scope", EXPERIENCES["pytest_fixture_scope"]["condition"], EXPERIENCES["pytest_fixture_scope"]["detail"], 36),
        ("async function tests", EXPERIENCES["pytest_asyncio"]["condition"], EXPERIENCES["pytest_asyncio"]["detail"], 36),
        ("Git patch application", EXPERIENCES["git_apply"]["condition"], EXPERIENCES["git_apply"]["detail"], 36),
    ]
    for title, condition, detail, repeats in specs:
        parts.append(_topic(title, condition, detail, repeats))
    text = "".join(parts)
    write_text(folder / "SKILL.md", text)
    existing = _topic("Existing import notes", EXPERIENCES["python_importerror"]["condition"],
                      "This file already records the import-path check.", 1)
    write_text(folder / "references" / "python_imports.md", existing)
    added = _topic("Additional patch evidence", EXPERIENCES["git_apply"]["condition"],
                   EXPERIENCES["git_apply"]["detail"], 18)
    payload = {
        "character_count": len(text),
        "existing_reference": "references/python_imports.md",
        "added_experience_characters": len(added),
        "projected_root_characters": len(text) + len(added),
        "hard_limit": 10000,
        "human_expectation": {
            "model_only_root_append_crosses_hard_limit": True,
            "program_migration_must_keep_root_at_or_below_hard_limit": True,
            "original_topic_sentences_must_remain_verbatim": [item[2] for item in specs],
            "rejected_migration_must_leave_the_published_pointer_unchanged": True,
        },
    }
    write_text(folder / "expected.json", json.dumps(payload, indent=2) + "\n")
    write_text(folder / "added_experience.md", added)
    return payload


def _s3(folder):
    text = FRONT.format(name="scenario-s3")
    text += "\n".join([
        "Read [Python debugging](references/python_debugging.md) for import and exception evidence.",
        "Read [testing strategies](references/testing_strategies.md) for pytest scope.",
        "Read [repository navigation](references/repository_navigation.md) for locating the failing file.",
        "",
    ])
    write_text(folder / "SKILL.md", text)
    refs = {
        "references/python_debugging.md": _topic(
            "Python debugging", EXPERIENCES["python_importerror"]["condition"],
            "Trace the missing module before editing callers.", 2),
        "references/testing_strategies.md": _topic(
            "Testing strategies", EXPERIENCES["pytest_fixture_scope"]["condition"],
            "Keep function-scoped fixtures out of session state.", 2),
        "references/repository_navigation.md": _topic(
            "Repository navigation", "When pytest names a failing file, open that file before searching the whole repository.",
            "Use the traceback path as the first location.", 2),
    }
    for path, body in refs.items():
        write_text(folder / path, body)
    payload = {
        "references": sorted(refs),
        "experiences": {
            "related": "pytest_fixture_scope",
            "partial": "python_importerror",
            "new_topic": "git_apply",
        },
        "human_expectation": {
            "related_update_should_target": "references/testing_strategies.md",
            "new_topic_file": "references/git_apply.md",
            "program_currently_accepts_a_second_file_with_overlapping_text": True,
        },
    }
    write_text(folder / "expected.json", json.dumps(payload, indent=2) + "\n")
    write_text(folder / "experiences.json", json.dumps(EXPERIENCES, indent=2) + "\n")
    return payload


def _s4(folder):
    same_scope = _topic(
        "Function fixture scope", EXPERIENCES["pytest_fixture_scope"]["condition"],
        "Create the resource inside the fixture and close it in teardown.", 1)
    shared = _topic(
        "Session fixture state",
        "When a pytest fixture uses session scope, preserve shared state for later tests.",
        "Do not reset a session resource inside a function-scoped test.", 1)
    async_base = _topic(
        "Coroutine tests", EXPERIENCES["pytest_asyncio"]["condition"],
        "Await the coroutine and then assert the returned value.", 1)
    async_extra = async_base + "Close the event loop after the assertion.\n"
    git = _topic(
        "Patch check", EXPERIENCES["git_apply"]["condition"],
        "Inspect the rejected hunk and rerun the focused pytest.", 1)
    refs = {
        "references/pytest_fixture_scope.md": same_scope,
        "references/pytest_fixture_scope_duplicate.md": same_scope,
        "references/pytest_session_state.md": shared,
        "references/async_tests.md": async_base,
        "references/async_tests_partial.md": async_extra,
        "references/git_apply.md": git,
    }
    text = FRONT.format(name="scenario-s4") + "\n".join(
        f"Read [{path}]({path})." for path in sorted(refs)) + "\n"
    write_text(folder / "SKILL.md", text)
    for path, body in refs.items():
        write_text(folder / path, body)
    payload = {
        "exact_duplicate": ["references/pytest_fixture_scope.md", "references/pytest_fixture_scope_duplicate.md"],
        "different_conditions": ["references/pytest_fixture_scope.md", "references/pytest_session_state.md"],
        "partial_duplicate": ["references/async_tests.md", "references/async_tests_partial.md"],
        "unique_sentence_that_must_survive": "Close the event loop after the assertion.",
        "unrelated": "references/git_apply.md",
        "human_expectation": {
            "exact_duplicate_may_be_deduplicated": True,
            "different_conditions_must_stay_separate": True,
            "partial_duplicate_must_not_drop_the_unique_sentence": True,
        },
    }
    write_text(folder / "expected.json", json.dumps(payload, indent=2) + "\n")
    return payload


def _s5(folder):
    text = FRONT.format(name="scenario-s5") + "Periodic maintenance starts from these two copies.\n"
    body = _topic(
        "Repeated pytest evidence", EXPERIENCES["pytest_fixture_scope"]["condition"],
        "Inspect the fixture scope before editing the test.", 1)
    write_text(folder / "SKILL.md", text)
    write_text(folder / "references" / "pytest_evidence.md", body)
    write_text(folder / "references" / "pytest_evidence_copy.md", body)
    steps = [{"task_id": f"task-{i:02d}", "kind": "accepted", "sentence": f"Verify task {i:02d} callers."} for i in range(9)]
    steps.append({"task_id": "task-09", "kind": "no_changes"})
    steps.append({"task_id": "task-10", "kind": "rejected"})
    steps.append({"task_id": "task-11", "kind": "accepted", "sentence": "Verify task 11 callers."})
    payload = {
        "duplicate_references": ["references/pytest_evidence.md", "references/pytest_evidence_copy.md"],
        "steps": steps,
        "human_expectation": {
            "accepted_updates_that_count": 10,
            "trigger_task": "task-11",
            "no_changes_and_rejected_do_not_count": ["task-09", "task-10"],
            "resume_must_not_repeat_the_maintenance_model_calls": True,
        },
    }
    write_text(folder / "expected.json", json.dumps(payload, indent=2) + "\n")
    return payload


def _s6(folder):
    text = FRONT.format(name="scenario-s6")
    text += "Select a reference from `/opt/rsi-skills/routing/index.yaml` when the loaded core does not cover the task.\n"
    topics = [
        ("import_error", "ImportError ModuleNotFoundError sys.path collection"),
        ("pytest_fixture", "pytest fixture function scope teardown"),
        ("pytest_asyncio", "pytest-asyncio coroutine await event loop"),
        ("git_apply", "git apply --check rejected hunk"),
        ("django_migrations", "django migration schema editor"),
        ("flask_routes", "flask route endpoint url_for"),
        ("numpy_shapes", "numpy ndarray reshape broadcasting"),
        ("pandas_index", "pandas DataFrame index alignment"),
        ("argparse_defaults", "argparse argument default namespace"),
        ("logging_config", "logging dictConfig handler formatter"),
        ("sqlalchemy_sessions", "sqlalchemy session commit rollback"),
        ("click_options", "click Option multiple values"),
        ("packaging_imports", "setuptools package_data wheel distribution"),
        ("typing_annotations", "typing Optional annotation checker"),
    ]
    queries = []
    for name, terms in topics:
        body = _topic(name.replace("_", " "), f"When the task mentions {terms.split()[0]}, use this note.",
                      f"The distinctive terms are {terms}.", 2 if name != "numpy_shapes" else 80)
        write_text(folder / "references" / f"{name}.md", body)
        if name != "numpy_shapes":
            queries.append({
                "id": name,
                "prompt": terms,
                "gold": [f"references/{name}.md"],
                "must_not_select": ["references/numpy_shapes.md"],
            })
    queries.append({
        "id": "ambiguous_import",
        "prompt": "ImportError setuptools package_data",
        "gold": ["references/import_error.md", "references/packaging_imports.md"],
        "must_not_select": ["references/django_migrations.md"],
    })
    write_text(folder / "SKILL.md", text)
    payload = {
        "reference_count": len(topics),
        "top_k": 2,
        "queries": queries,
        "large_reference": "references/numpy_shapes.md",
        "unselected_sentinel_term": "ndarray reshape broadcasting",
        "human_expectation": {
            "clear_queries_should_rank_their_gold_file_first": True,
            "top_k_is_two_complete_files": True,
            "five_solvers_share_one_route_snapshot": True,
            "budget_skips_an_oversized_file_instead_of_truncating_it": True,
        },
    }
    write_text(folder / "expected.json", json.dumps(payload, indent=2) + "\n")
    return payload


if __name__ == "__main__":
    destination = Path(__file__).resolve().parent / "fixtures" / "skill_scenarios"
    built = build(destination)
    for name, payload in built.items():
        count = payload.get("character_count")
        print(name, count if count is not None else payload.get("reference_count"))
