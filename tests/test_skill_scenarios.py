"""Offline Skill-management scenarios. Mock clients supply decisions; program code is real."""
import json
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from rsi import ROOT
from rsi.agent.runner import run_experiment
from rsi.core.common import digest, read_json
from rsi.skills.access import Snapshot, install, settings as access_settings
from rsi.skills.budget import render_context, select_context
from rsi.skills.consolidation import consolidate
from rsi.skills.directory import INDEX_PATH, read_skill_directory
from rsi.skills.index import build_index, validate_index
from rsi.skills.maintenance import MaintenanceRejected, maintain_candidate, settings as maintenance_settings
from rsi.skills.markdown_units import parse_units
from rsi.skills.routing import retrieve_bm25, route
from rsi.skills.store import SkillStore, load_initial_skill
from routing_fixtures import CharacterCounter
from test_core_baseline import Backend, Models, test_config
from test_maintenance_transactions import LocalEnvironment
from test_reference_consolidation import plan_for
from test_skill_migration import PlannerModels, plan_for as plan_for_migration
from test_skill_routing import ranked


FIXTURES = ROOT / "tests" / "fixtures" / "skill_scenarios"


def expected(name):
    return json.loads((FIXTURES / name / "expected.json").read_text(encoding="utf-8"))


def scenario_files(name):
    folder = FIXTURES / name
    files, _ = load_initial_skill(directory_schema=1, bootstrap=True)
    files["SKILL.md"] = (folder / "SKILL.md").read_text(encoding="utf-8")
    for path in folder.glob("references/*.md"):
        files["references/" + path.name] = path.read_text(encoding="utf-8")
    files[INDEX_PATH] = build_index(files)
    validate_index(files)
    return files


def managed_config(**extra):
    config = test_config()
    config["skill_directory"] = {"schema": 1}
    config["skill_maintenance"] = {"enabled": True}
    config["skill_editor_model"] = dict(config["skill_editor_model"])
    config.update(extra)
    return config


def publish(root, files, maintenance=True):
    options = maintenance_settings({"skill_maintenance": {"enabled": maintenance}})
    return SkillStore(root, files, directory_schema=1, maintenance=options).current()


class FixtureTests(unittest.TestCase):
    def test_published_skill_is_not_the_fixture_and_builtin_stays_untouched(self):
        builtin = ROOT / "skills" / "test-and-diagnose" / "SKILL.md"
        before = builtin.read_bytes()
        for name in ("s1_short", "s2_near_limit", "s3_three_references", "s4_redundancy", "s5_periodic", "s6_routing"):
            files = scenario_files(name)
            self.assertNotEqual(files["SKILL.md"], before.decode("utf-8"))
            self.assertIn("scripts/inspect_project.py", files)
        self.assertEqual(builtin.read_bytes(), before)

    def test_s1_and_s2_lengths_match_the_human_labels(self):
        s1 = expected("s1_short")
        s2 = expected("s2_near_limit")
        self.assertEqual(s1["character_count"], len(scenario_files("s1_short")["SKILL.md"]))
        self.assertTrue(2000 <= s1["character_count"] <= 3000)
        self.assertEqual(s2["character_count"], len((FIXTURES / "s2_near_limit" / "SKILL.md").read_text(encoding="utf-8")))
        self.assertTrue(9000 <= s2["character_count"] <= 9500)
        self.assertGreater(s2["projected_root_characters"], 10000)
        self.assertEqual(s2["existing_reference"], "references/python_imports.md")


class UpdaterBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = __import__("tempfile").TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        Backend.barrier, Backend.instances = threading.Barrier(5), []
        self._patch_fake_install()
        patcher = patch("rsi.agent.runner.functional_gate", return_value={"passed": True})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _patch_fake_install(self):
        import hashlib
        from test_core_baseline import Environment
        original = Environment.execute

        def execute(env, command, cwd=""):
            if "RSI_MANIFEST=" in command:
                hashes = {path: hashlib.sha256(text.encode()).hexdigest() for path, text in env.skill.items()}
                return {"returncode": 0, "output": "RSI_MANIFEST=" + json.dumps(hashes)}
            return original(env, command, cwd)

        mock = patch.object(Environment, "execute", execute)
        mock.start()
        self.addCleanup(mock.stop)
        mock = patch.object(Environment, "install_skill", lambda env, folder: setattr(
            env, "skill", read_skill_directory(folder, directory_schema=1)), create=True)
        mock.start()
        self.addCleanup(mock.stop)

    def run_updater(self, name, factory):
        files = scenario_files(name)
        skill = publish(self.root / name, files)
        config = managed_config()
        config["initial_skill"] = str(skill)
        tasks = [{"id": "scenario", "benchmark": "swe", "prompt": "Fix the visible ImportError"}]
        output = self.root / (name + "-run")
        reports = run_experiment(tasks, config, output, backend_factory=Backend, model_factory=factory)
        update = read_json(output / "scenario" / "update.json")
        after = read_skill_directory(output / "skills" / "versions" / update["after_skill_version"], directory_schema=1)
        maintenance = read_json(output / "scenario" / "maintenance" / "validation.json")
        return reports, update, after, maintenance

    def test_s1_model_can_create_a_reference_without_p1_migration(self):
        detail = json.loads((FIXTURES / "s1_short" / "experiences.json").read_text(encoding="utf-8"))["python_importerror"]["detail"]

        class Creating(Models):
            def __call__(owner, config):
                client = super().__call__(config)
                query = client.query

                def respond(messages, **kwargs):
                    answer = query(messages, **kwargs)
                    if client.name == "judge" and answer["content"].startswith("```json"):
                        data = json.loads(answer["content"][7:-3])
                        data["edits"] = [
                            {"file": "SKILL.md", "op": "append_to_section", "target_section": "# Test and diagnose",
                             "content": "For import failures, read [details](references/python_imports.md)."},
                            {"file": "references/python_imports.md", "op": "create", "content": "# Imports\n" + detail},
                        ]
                        answer["content"] = "```json\n" + json.dumps(data) + "\n```"
                    return answer

                client.query = respond
                return client

        _, update, after, maintenance = self.run_updater("s1_short", Creating())
        self.assertTrue(update["accepted"])
        self.assertIn(detail, after["references/python_imports.md"])
        self.assertIn("references/python_imports.md", after["SKILL.md"])
        self.assertEqual(maintenance["events"], 0)
        self.assertLess(len(after["SKILL.md"]), 10000)
        self.assertEqual(validate_index(after)["entries"][0]["path"], "references/python_imports.md")

    def test_s1_root_only_edit_does_not_cause_the_program_to_create_a_reference(self):
        _, update, after, maintenance = self.run_updater("s1_short", Models())
        self.assertTrue(update["accepted"])
        self.assertFalse(any(path.startswith("references/") for path in after))
        self.assertEqual(maintenance["events"], 0)
        self.assertLess(maintenance["root_chars"], 10000)


class MaintenanceScenarioTests(unittest.TestCase):
    def setUp(self):
        self.tmp = __import__("tempfile").TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = managed_config()

    def test_s2_program_moves_a_verbatim_section_and_a_rejected_plan_does_not_publish(self):
        files = scenario_files("s2_near_limit")
        labels = expected("s2_near_limit")
        added = (FIXTURES / "s2_near_limit" / "added_experience.md").read_text(encoding="utf-8")
        proposed = files["SKILL.md"] + added
        self.assertGreater(len(proposed), 10000)
        movable = [unit for unit in parse_units(proposed) if not unit.protected and unit.applicability]
        self.assertTrue(movable)
        store = SkillStore(self.root / "store", files, directory_schema=1,
                           maintenance=maintenance_settings(self.config))
        published = store.current().name
        result = maintain_candidate(files, {"SKILL.md": proposed}, self.config, self.root / "migrate",
                                    model_factory=PlannerModels(planner=lambda data: plan_for_migration(
                                        data, "references/click_case.md", limit=8,
                                        select=lambda unit: "Python ImportError recovery" in unit["heading_path"])))
        self.assertLessEqual(len(result["files"]["SKILL.md"]), labels["hard_limit"])
        self.assertIn("references/click_case.md", result["changes"])
        self.assertNotIn("references/click_case.md", files)
        blob = "\n".join(result["files"][path] for path in result["files"] if path.endswith(".md"))
        for sentence in labels["human_expectation"]["original_topic_sentences_must_remain_verbatim"]:
            self.assertIn(sentence, blob)
        self.assertIn("routing/index.yaml", result["managed_changes"])
        self.assertNotIn(INDEX_PATH, result["changes"])

        def refuse(data):
            return plan_for_migration(data, limit=0)

        with self.assertRaises(MaintenanceRejected):
            maintain_candidate(files, {"SKILL.md": proposed}, self.config, self.root / "reject",
                               model_factory=PlannerModels(planner=refuse))
        self.assertEqual(store.current().name, published)
        self.assertEqual(read_skill_directory(store.current(), directory_schema=1)["SKILL.md"], files["SKILL.md"])

    def test_s2_model_supplied_reference_is_not_relabeled_as_program_migration(self):
        files = scenario_files("s2_near_limit")
        shortened = files["SKILL.md"][:8000] + "\n"
        created = "# Imports\nWhen Python raises `ImportError` during pytest collection, preserve the package layout.\n"
        models = PlannerModels()
        result = maintain_candidate(files, {"SKILL.md": shortened, "references/python_imports.md": created},
                                    self.config, self.root / "model-owned", model_factory=models)
        self.assertEqual(models.requests, [])
        self.assertEqual(result["files"]["references/python_imports.md"], created)
        self.assertLessEqual(len(result["files"]["SKILL.md"]), 10000)

    def test_s3_related_update_new_topic_and_unchecked_overlap(self):
        files = scenario_files("s3_three_references")
        labels = expected("s3_three_references")
        experiences = json.loads((FIXTURES / "s3_three_references" / "experiences.json").read_text(encoding="utf-8"))
        related = files["references/testing_strategies.md"] + "\n" + experiences["pytest_fixture_scope"]["detail"] + "\n"
        models = PlannerModels()
        updated = maintain_candidate(files, {"references/testing_strategies.md": related}, self.config,
                                     self.root / "related", model_factory=models)
        self.assertEqual(models.requests, [])
        self.assertEqual(sorted(path for path in updated["files"] if path.startswith("references/")), labels["references"])
        self.assertIn(experiences["pytest_fixture_scope"]["detail"], updated["files"]["references/testing_strategies.md"])

        new_body = "# Git\n" + experiences["git_apply"]["detail"] + "\n"
        guide = files["SKILL.md"] + "\nRead [git apply](references/git_apply.md).\n"
        created = maintain_candidate(files, {"SKILL.md": guide, "references/git_apply.md": new_body}, self.config,
                                     self.root / "new-topic", model_factory=PlannerModels())
        self.assertIn("references/git_apply.md", created["files"])
        self.assertEqual(created["files"]["references/git_apply.md"], new_body)
        self.assertIn("references/git_apply.md", {entry["path"] for entry in validate_index(created["files"])["entries"]})

        overlap = "# Extra\n" + experiences["python_importerror"]["detail"] + "\n"
        guide = files["SKILL.md"] + "\nRead [extra](references/python_debugging_extra.md).\n"
        duplicated = maintain_candidate(
            files, {"SKILL.md": guide, "references/python_debugging_extra.md": overlap}, self.config,
            self.root / "overlap", model_factory=PlannerModels())
        self.assertIn("references/python_debugging.md", duplicated["files"])
        self.assertIn("references/python_debugging_extra.md", duplicated["files"])
        self.assertIn("Trace the missing module before editing callers.", duplicated["files"]["references/python_debugging.md"])
        self.assertIn(experiences["python_importerror"]["detail"], duplicated["files"]["references/python_debugging_extra.md"])


class RedundancyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = __import__("tempfile").TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.files = scenario_files("s4_redundancy")
        self.labels = expected("s4_redundancy")
        self.config = managed_config(reference_maintenance={"enabled": True})

    def stage(self, plan, name):
        models = PlannerModels(planner=lambda data, plan=plan: plan)
        out = consolidate(self.files, self.config, self.root / name, model_factory=models)
        return out, read_json(self.root / name / "validation.json")

    @property
    def root(self):
        return Path(self.tmp.name)

    def test_s4_exact_duplicate_partial_duplicate_and_different_conditions(self):
        discovered = __import__("rsi.skills.consolidation", fromlist=["discover"]).discover(
            self.files, __import__("rsi.skills.consolidation", fromlist=["settings"]).settings(self.config))
        pair = set(self.labels["exact_duplicate"])
        self.assertTrue(any(set(item["sources"]) == pair and item["kind"] == "exact_file" for item in discovered["pairs"]))

        deduped, dedup_receipt = self.stage(
            plan_for(self.files, "DEDUPLICATE", self.labels["exact_duplicate"], self.labels["exact_duplicate"][0]), "dedup")
        self.assertEqual(dedup_receipt["status"], "consolidated")
        self.assertIn(self.labels["exact_duplicate"][0], deduped)
        self.assertNotIn(self.labels["exact_duplicate"][1], deduped)
        self.assertIn(self.labels["unique_sentence_that_must_survive"], deduped["references/async_tests_partial.md"])

        conflict, conflict_receipt = self.stage(
            plan_for(self.files, "MERGE", self.labels["different_conditions"], "references/merged_scope.md"), "conflict")
        self.assertEqual(conflict, self.files)
        self.assertEqual(conflict_receipt["status"], "consolidation_failed")
        self.assertEqual(conflict_receipt["decision"], "KEEP_SEPARATE")

        partial, partial_receipt = self.stage(
            plan_for(self.files, "DEDUPLICATE", self.labels["partial_duplicate"], self.labels["partial_duplicate"][0]), "partial")
        self.assertEqual(partial, self.files)
        self.assertEqual(partial_receipt["status"], "consolidation_failed")
        self.assertIn(self.labels["unique_sentence_that_must_survive"], partial["references/async_tests_partial.md"])
        self.assertIn("references/git_apply.md", partial)


class PeriodicTests(unittest.TestCase):
    def setUp(self):
        self.tmp = __import__("tempfile").TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.output = Path(self.tmp.name) / "run"
        self.labels = expected("s5_periodic")
        self.steps = {item["task_id"]: item for item in self.labels["steps"]}
        self.files = scenario_files("s5_periodic")
        self.source = publish(Path(self.tmp.name) / "source", self.files)
        self.config = managed_config(reference_maintenance={"enabled": True})
        self.config["initial_skill"] = str(self.source)
        self.tasks = [{"id": item["task_id"], "benchmark": "swe", "prompt": "Fix visible pytest fixture scope"} for item in self.labels["steps"]]
        self.planner = PlannerModels(planner=self.make_plan)
        self.models = Models()
        Backend.barrier, Backend.instances = threading.Barrier(5), []
        UpdaterBoundaryTests._patch_fake_install(self)
        for target, value in (("rsi.agent.runner.functional_gate", {"passed": True}),):
            mock = patch(target, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)
        mock = patch("rsi.agent.runner.evolve_skill", side_effect=self.learn)
        mock.start()
        self.addCleanup(mock.stop)

    def learn(self, task, results, skill, *args, **kwargs):
        step = self.steps[task["id"]]
        if step["kind"] == "no_changes":
            return {"changes": {}, "reason": "no new experience"}
        if step["kind"] == "rejected":
            return {"changes": {"scripts/inspect_project.py": "bad"}, "reason": "illegal", "evidence_rollouts": list(range(5))}
        text = (skill / "SKILL.md").read_text(encoding="utf-8")
        return {"changes": {"SKILL.md": text + "\n" + step["sentence"] + "\n"},
                "reason": "mock effective learning", "evidence_rollouts": list(range(5))}

    def make_plan(self, data):
        seed = next(read_json(path)["files"] for path in self.output.glob("task-*/consolidation/savepoint.json")
                    if read_json(path)["version"] == data["input_hash"])
        return plan_for(seed, "DEDUPLICATE", self.labels["duplicate_references"], self.labels["duplicate_references"][0])

    def factory(self, options):
        basic = self.models(options)
        stage = self.planner(options)
        query, stats = basic.query, basic.stats

        def dispatch(messages, **kwargs):
            if messages[0]["content"].startswith(("Propose ONE bounded", "Independently review this lossless")):
                stage.deadline = basic.deadline
                return stage.query(messages, **kwargs)
            return query(messages, **kwargs)

        basic.query = dispatch
        basic.stats = lambda: {key: stats()[key] + stage.stats()[key] for key in stats()}
        return basic

    def test_s5_counts_only_accepted_updates_and_resume_does_not_repeat_maintenance(self):
        run_experiment(self.tasks, self.config, self.output, backend_factory=Backend, model_factory=self.factory)
        state = read_json(self.output / "checkpoint.json")
        self.assertEqual(state["reference_maintenance"], {"accepted_updates": 10, "checks": 1, "cursor": 1})
        due = [entry["report"]["task_id"] for entry in state["completed"] if entry["update"]["reference_maintenance"]["due"]]
        self.assertEqual(due, [self.labels["human_expectation"]["trigger_task"]])
        ignored = [entry for entry in state["completed"] if entry["report"]["task_id"] in ("task-09", "task-10")]
        self.assertTrue(all(not entry["update"]["accepted"] for entry in ignored))
        final = state["completed"][-1]["update"]
        self.assertEqual(final["consolidation"]["status"], "consolidated")
        published = read_skill_directory(self.output / "skills" / "versions" / final["after_skill_version"], directory_schema=1)
        self.assertIn(self.labels["duplicate_references"][0], published)
        self.assertNotIn(self.labels["duplicate_references"][1], published)
        calls = len(self.planner.requests)
        self.assertEqual(calls, 2)
        run_experiment(self.tasks, self.config, self.output, resume=True, backend_factory=Backend, model_factory=self.factory)
        self.assertEqual(len(self.planner.requests), calls)
        self.assertEqual(read_skill_directory(self.source, directory_schema=1), self.files)


class RoutingScenarioTests(unittest.TestCase):
    def setUp(self):
        self.tmp = __import__("tempfile").TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.files = scenario_files("s6_routing")
        self.labels = expected("s6_routing")
        self.skill = publish(Path(self.tmp.name) / "skill", self.files, maintenance=False)

    def test_s6_labeled_recall_budget_and_five_route_snapshot(self):
        index = validate_index(self.files)
        recalls = []
        for query in self.labels["queries"]:
            chosen = [item.path for item in ranked(query["prompt"], self.files) if item.score > 0 and not item.exclusion][:self.labels["top_k"]]
            gold = set(query["gold"])
            recalls.append(len(set(chosen) & gold) / len(gold))
            if len(gold) == 1:
                self.assertEqual(chosen[:1], query["gold"])
            self.assertTrue(gold <= set(chosen))
            self.assertTrue(set(query["must_not_select"]).isdisjoint(chosen))
        self.assertEqual(recalls, [1.0] * len(recalls))

        core = len(render_context(self.files))
        small = "references/import_error.md"
        large = self.labels["large_reference"]
        limit = core + len(self.files[small]) + 400
        self.assertGreater(len(render_context(self.files, [large])), limit)
        selection = select_context(self.files, [large, small], CharacterCounter("solver"), budget_limit=limit, top_k=2)
        self.assertEqual(selection.context.selected_references, (small,))
        self.assertEqual(selection.excluded[0][0], large)
        self.assertEqual(selection.excluded[0][1], "reference_over_budget")
        self.assertLessEqual(selection.context.initial_skill_tokens, limit)
        self.assertNotIn(self.labels["unselected_sentinel_term"], selection.context.text)

        config = managed_config(skill_maintenance={"enabled": False}, skill_routing={
            "enabled": True, "top_k": 2, "max_initial_skill_tokens": 20000, "budget_mode": "strict"})
        config["model"]["name"] = "mock-solver"
        routed = route({"id": "import-task", "prompt": self.labels["queries"][0]["prompt"]}, self.skill, config,
                       counter_factory=lambda item: CharacterCounter(item["name"]))
        self.assertEqual(routed.context.selected_references[0], "references/import_error.md")
        self.assertIn("/opt/rsi-skills/references/", routed.context.text)
        self.assertIn("routing/index.yaml", routed.context.text)

        from test_skill_routing import RoutingIntegrationTests
        case = RoutingIntegrationTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.seed = self.skill
        case.config["initial_skill"] = str(self.skill)
        case.config["skill_routing"]["top_k"] = 2
        case.tasks[0]["prompt"] = self.labels["queries"][0]["prompt"]
        from rsi.skills.routing import route as real_route
        from rsi.agent.execution import execute_one as real_execute
        from concurrent.futures import ThreadPoolExecutor
        contexts = []

        def execute(*args, **kwargs):
            contexts.append(kwargs["skill_context"])
            return real_execute(*args, **kwargs)

        with patch("rsi.agent.runner.route_skill", wraps=real_route), \
                patch("rsi.agent.runner.execute_one", side_effect=execute), \
                patch("rsi.agent.runner.ThreadPoolExecutor", side_effect=lambda *args, **kwargs: ThreadPoolExecutor(*args, **kwargs)):
            case.run_tasks()
        self.assertEqual(len(contexts), 5)
        self.assertTrue(all(item.context_hash == contexts[0].context_hash for item in contexts))
        self.assertNotIn(self.labels["unselected_sentinel_term"], contexts[0].text)


class ProtectionScenarioTests(unittest.TestCase):
    def test_s6_copy_is_explicitly_degraded_and_strong_read_only_is_not_silently_downgraded(self):
        files = scenario_files("s6_routing")
        root = Path(__import__("tempfile").mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(root))
        skill = publish(root / "skill", files, maintenance=False)
        snapshot = Snapshot(skill)
        self.addCleanup(snapshot.cleanup)
        options = access_settings({"skill_directory": {"schema": 1}, "skill_routing": {"enabled": True},
                                    "skill_update_method": "trace2skill", "skill_access": {"enabled": True}})
        receipt = install(LocalEnvironment(root / "installed"), snapshot, options, root / "copy")
        self.assertEqual(receipt["status"], "verified")
        self.assertTrue(receipt["degraded"])
        self.assertFalse(receipt["read_only_enforced"])
        self.assertEqual(read_skill_directory(root / "installed", directory_schema=1), files)
        with self.assertRaisesRegex(RuntimeError, "downgrade"):
            install(LocalEnvironment(root / "unused"), snapshot, options | {"require_read_only": True}, root / "strict")
        self.assertFalse((root / "unused").exists())
