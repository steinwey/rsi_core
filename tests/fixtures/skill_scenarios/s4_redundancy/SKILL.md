---
name: scenario-s4
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

Read [references/async_tests.md](references/async_tests.md).
Read [references/async_tests_partial.md](references/async_tests_partial.md).
Read [references/git_apply.md](references/git_apply.md).
Read [references/pytest_fixture_scope.md](references/pytest_fixture_scope.md).
Read [references/pytest_fixture_scope_duplicate.md](references/pytest_fixture_scope_duplicate.md).
Read [references/pytest_session_state.md](references/pytest_session_state.md).
