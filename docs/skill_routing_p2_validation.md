# P2 acceptance report — finalized 2026-11-05

## Git and scope

Worktree: D:/RSI-core-skill-routing. Branch: feature/skill-maintenance-routing.
Starting HEAD: 0437ee7a124ce6729d94ebc51470304e5e350d50, clean before P2.
Actual diff from skill-routing-p1 (5f9329b47783a012d770b45e06cc8348b7273d63)
was one removed terminal blank line in maintenance.py. P0 tag remains
86512bb6d47fa68772bcec561d1d257711b9bbbd; baseline/main remains
4fd9948d8ac36560025e105aca1430c82d8714b6. Accepted P2 code is identified by
skill-routing-p2. No main merge or remote push is performed.

New modules: rsi/skills/routing.py and budget.py. Integration touches directory/config
validation, runner, execution and checkpoint receipt checks. New tests are
routing_fixtures.py, test_skill_budget.py and test_skill_routing.py. P0/P1 implementation
modules (store, index, maintenance, migration, markdown_units), old tests and docs,
benchmark adapters, formal scoring, upstream vendor and mini-SWE source are unchanged.
README adds a current P2 entry. No historical run, Skill or task manifest is modified.

## Regression

Executed on 2026-11-04:

```powershell
Set-Location D:/RSI-core-skill-routing
$env:TEMP='D:/RSI-core/_work/p2-tests'
$env:TMP=$env:TEMP
$env:PYTHONUTF8='1'
$env:LITELLM_LOCAL_MODEL_COST_MAP='True'
& D:/RSI/.venv-py314/Scripts/python.exe -m unittest discover -s tests -q
```

**283 tests: 282 passed, 0 failed/errors, 1 skipped; 52.363 seconds.**
All 222 previous tests remain, with 61 new P2 tests. The sole skip is the existing
Windows real-symlink privilege limitation. No dependencies were skipped. Full log:
D:/RSI-core/_work/p2-tests/full-suite-1.log. The earlier targeted run passed 124 tests
with the same single skip; its log remains targeted-1.log.

Coverage includes standard BM25 formula, exact/code/error/module identifiers, casing,
CJK, field weighting, exclusions, zero matches, stable ties, hashes and immutability;
whole-context counting, exact 4000/4001 boundaries, skip-large-try-small, whole-file
preservation, core failure, counter identity/precision/errors and final recount;
once-before-pool routing, the same object and identical actual system text across five
independent solver trajectories, full unselected-file installation, unchanged Selector
and formal grades, single-route frozen evaluation and exact disabled injection;
combined P1/P2, next-task committed Skill inheritance, schema 5/6 and ReasoningBank
regressions, publication failure recovery without repeated calls, precommit archival,
changed-config rejection and route/context tampering rejection.

All models/backends/counters in new integration tests are offline test doubles. No paid
API, remote tokenizer, live benchmark or Docker/Harbor experiment was run. Upstream
alignment and old regression assertions remain intact. Runtime counters have no claimed
strict support for the three real model names; tests do not certify their tokenization.

## Retained Mock example

Directory: D:/RSI-core/_work/p2-tests/mock-example.
Summary: example-summary.json; task artifacts: task/skill-route.json and
skill-context.txt; five trajectory files retain actual injected messages.

Raw visible task prompt: `pytest fixture` (mock SWE task, no scoring information).

| Candidate | BM25 score | Decision |
|---|---:|---|
| references/pytest.md | 1.8100557831915585 | Load complete file |
| references/other.md | 0.0 | Exclude: no positive match |

Initial SkillContext: **1883 synthetic test units / 4000**. Counter identity:
test-unicode-codepoint-v1; accuracy=test_only. These are explicitly NOT measured
Luna/Qwen tokens. Route ID:
`e951d4dd246e9af584460c4d4c0f87fcfb4f67d953dcdc879bc9cea9b38c5e9c`.
Context SHA256:
`8277d17868ea70304e6bfd62c52592e2369f57d1d08a9ea070b9594e0a760c25`.
Five mock solver requests shared this context; five independent patch artifacts were
preserved. The test asserts actual injected text, IDs and installed unselected files.
This example is not model-quality, benchmark-success or generalization evidence.

## Remaining limits

No verified bundled exact counter for gpt-6-luna/qwen3.8-max/qwen3.8-flash: strict
routing fails closed pending a reviewed adapter. BM25 and exclusion phrases are lexical,
not semantic applicability judgments. Whole-file only; index units are not trusted for
partial injection. No live Docker/Harbor or privileged symlink verification. No P3/P4.
See skill_routing_p2.md for detailed algorithms, capability sources and exact safe
rollback/worktree commands. Git code tags do not restore runtime experiment state.
