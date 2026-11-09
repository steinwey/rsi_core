---
name: scenario-s5
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

Periodic maintenance starts from these two copies.
