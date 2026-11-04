# P1 acceptance report — 2026-11-04

## Baseline and scope

- Repository: steinwey/rsi_core.
- Development worktree: `D:/RSI-core-skill-routing`.
- Branch: `feature/skill-maintenance-routing`.
- Starting HEAD and P0 tag: `86512bb6d47fa68772bcec561d1d257711b9bbbd`.
- The starting working tree was clean. main, P0 commit/tag and P0 documentation were not changed.
- Accepted P1 code is identified by the new `skill-routing-p1` tag. No P2/P3/P4 implementation or experiments were started.

## Files

New:

- `rsi/skills/markdown_units.py`: conservative heading/fence-aware spans, core protection, link rebasing.
- `rsi/skills/migration.py`: strict MigrationPlan, exact coverage, deterministic reference splitting.
- `rsi/skills/maintenance.py`: config, deadlines/call limits, planning/review, isolated maintenance, logs and size gates.
- `tests/test_skill_migration.py`: 40 new unit/integration tests.
- `docs/skill_routing_p1.md` and this report.

Modified:

- `rsi/agent/runner.py`: bootstrap and post-Trace2Skill maintenance, usage and receipts.
- `rsi/core/config.py`, `rsi/skills/directory.py`: explicit enablement and strict P1 configuration.
- `rsi/skills/index.py`: opt-in append-only provenance retention; P0 default unchanged.
- `rsi/skills/store.py`: repeated canonical/size gates using the existing atomic transaction.
- `rsi/experiment/checkpoints.py`: P1 limits and artifact receipt verification; schema remains 6.
- `README.md`: current P1 documentation entry.

## Test commands and results

```powershell
Set-Location D:/RSI-core-skill-routing
$env:TEMP='D:/RSI-core/_work/p1-tests'
$env:TMP=$env:TEMP
$env:PYTHONUTF8='1'
$env:LITELLM_LOCAL_MODEL_COST_MAP='True'
& D:/RSI/.venv-py314/Scripts/python.exe -m unittest discover -s tests -q
```

Final run: **222 tests; 221 passed, 0 failed/errors, 1 skipped; 41.482 seconds**.
The 40 new P1 tests passed. All original P0/legacy tests and assertions are unchanged.
The only skip remains the Windows real-symlink privilege limitation from P0; injected
symlink detection remains tested. No dependency-related skips occurred.

Full log: `D:/RSI-core/_work/p1-tests/final-suite-2.log`.
Earlier run/error logs remain in the same directory. An earlier no-progress fixture
accidentally selected a newly protected ancestor; its input was corrected to exercise
the intended no-progress branch, retaining the failure assertion. No existing test was
removed or weakened.

Tests use Mock LLM, Fake Backend and temporary directories. The real pinned Trace2Skill
MAP/REDUCE/Apply pipeline is exercised before migration in the integration test. No paid
API, live benchmark evaluation, real Docker/Harbor run or dataset mutation was performed.

## Acceptance coverage

- 9999/10000 characters stay unchanged; 10001 triggers bounded migration/rejection.
- Successful specific-section migration, root hard limit and preferred soft target.
- Protected core and protected descendants retained; mixed parents retain general
  constraints while independently scoped children move with copied context.
- YAML frontmatter, ATX/Setext headings, complete fences, lists/quotes, Unicode/BOM/CRLF.
- Source and target spans/hashes, missing coverage, rewriting and duplicate JSON keys.
- Existing destination append, new reference creation, spill into a sibling when full.
- Oversized reference split with navigation retained; indivisible content rejected.
- Markdown relative path and anchor repair; ambiguous links/code/HTML/cross-section
  dependencies rejected; independent reviewer uncertainty rejects both root moves and splits.
- Source provenance identity retained on proven append-only extensions; no invented PASS.
- Invalid plans, nonconvergence, no progress, strict call caps and deadline expiry.
- API, disk and validator runtime faults distinguished from ordinary rejection.
- Initial source stays unchanged during bootstrap; failed bootstrap is marked failed
  without a checkpoint or any task execution.
- Store/gate limits cannot be bypassed by directly proposing oversized documents.
- Rejected maintenance commits completed formal scores and retains current Skill.
- Precommit failure retains pointer; committed-but-unpublished tasks resume without
  repeating migration/model calls; incomplete attempts and original scores are archived.
- Modified coverage artifacts reject resume. Disabled P1 keeps legacy long-Skill behavior.
- P0 Schema 5/6, ReasoningBank, PASS gate, upstream alignment and five-route regressions passed.

## Retained synthetic examples

These are Mock-only examples, not model-quality or benchmark evidence.

`D:/RSI-core/_work/p1-tests/examples-fnp4sj01/summary.json`

| Example | Outcome |
|---|---|
| Three scoped Click sections, 10,670-char root | Root 7,549 chars; new reference 4,031 chars; exact coverage accepted |
| 16,116-char unscoped generic content | Rejected: no safely scoped movable sections; no candidate version published |

The success example used two mock requests (plan and independent review). The example
usage numbers are synthetic test values, not real API token/cost measurements. Detailed
input/output/coverage/normalization/validation artifacts are retained beside the summary.

## Diff and preservation checks

`git diff --check` passed. Pinned vendor manifest SHA256 checks passed. There are no
changes to vendor, external mini-SWE source, original Skill, generic file_map, Action
protocol, formal scoring/benchmark adapters, tasks, P0 docs, existing tests, historical
runs/checkpoints/versions, local experiment configuration or credentials. No main merge
or remote push was performed in this phase.

## Limits and next step

No known failing P1 test remains. Parsing/classification is deliberately conservative;
ambiguous or unstructured long documents may be rejected rather than forcibly shortened.
Independent LLM review is an additional guard, not a proof of arbitrary semantic equivalence
or improved task accuracy. Semantic rewriting/summarization is unsupported.

New/split references retain source lineage in the coverage ledger and archived input index;
they do not infer unit-level success provenance from broad historical file-level claims.
P1 does not authenticate externally supplied legacy evidence. Index unit declarations remain
untrusted for future partial routing; P2 must use verified units or whole-file selection.

Live model quality, real container installation and privileged filesystem tests remain
outside this offline acceptance. Bootstrap model usage is reported separately from task
metrics. A bootstrap failure has no task progress to resume and requires a new output
directory; already initialized runs retain normal checkpoint recovery.

See `skill_routing_p1.md` for the actual algorithm, public integration interfaces and exact
P0 rollback commands. Stop after the P1 commit/tag; require explicit “继续 P2” before P2.
