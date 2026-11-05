# P3 validation report (2026-11-05)

Implementation worktree: `D:/RSI-core-skill-routing`; branch: `feature/skill-maintenance-routing`.
Parent: `a2526cfb4ceb6552e4de097dd6e54caf27d7907f` (`skill-routing-p2-tokenizer`).
P3 is committed/tagged locally only after final regression passes. The new stable tag is
`skill-routing-p3`; use `git rev-parse skill-routing-p3^{commit}` for its exact commit.
No remote push, main merge, historical tag replacement, paid API call, Jev or P4 implementation.

## Executed Mock examples

All counts below come from deterministic Mock clients (10 input / 5 output tokens per
call), not provider usage or a real cost estimate. Local observed durations exclude solver
and learning time and cannot predict real API latency. Complete source/output maps,
candidate sets, calls, coverage and validation are retained under
`D:/RSI-core/_work/p3-tests/examples-final/`. Earlier example attempts remain under `examples/`.
The tracked portable report is [reference_consolidation_mock.json](examples/reference_consolidation_mock.json).

| Example | References | Reference characters | Skill version before → after | Calls / input / output | Seconds |
|---|---|---|---|---|---|
| merge | 2 → 1 | 111 → 112 | `577cdf3b0cd8d011` → `872b601cce9b73df` | 2 / 20 / 10 | 0.034 |
| condition-conflict | 2 → 2 | 125 → 125 | `f3371454a05db448` → `f3371454a05db448` | 3 / 30 / 15 | 0.036 |
| tenth-failure | 2 → 2 | 111 → 111 | `3bf38c04c2efe0cb` → `3bf38c04c2efe0cb` | 3 / 30 / 15 | 0.025 |

- Merge: A and B share the same explicit pytest condition. Concatenation preserves both
  complete bodies and provenance; the extra character is the joining newline.
- Conditional rejection: B applies to test isolation/shared state, A to pytest evidence.
  The proposed merge is rejected three times. Final decision is `KEEP_SEPARATE`; neither
  original changes. Separate tests also cover explicit `CONFLICT_UNRESOLVED` and an
  independent reviewer veto for contradictory instructions.
- Tenth-update recovery: the ninth published version is `ac522edd4bd6419b`; the tenth
  mandatory savepoint is `3bf38c04c2efe0cb`. Three invalid JSON attempts result in
  `consolidation_failed`, but learning remains accepted and the savepoint is published.
  The committed counter is `{accepted_updates: 10, checks: 1, cursor: 1}`. Resume leaves
  consolidation call count at 3; all ten tasks remain committed.

Full reference SHA256 values (unchanged references retain these exact hashes):

| Example / file | Characters | SHA256 |
|---|---|---|
| merge: `references/a.md` | 54 | `42c03ceb9e4cb79ddab29f295bb5d65bc48c6b1ee74453abf0b7fb45a98da3a6` |
| merge: `references/b.md` | 57 | `dc98348cf390d1a9b3ac600d369f51bb5b31a674624472046ba4e4cd7a32c2f0` |
| merge: `references/merged.md` | 112 | `f8b5d2ec6913fb1a258ab4534d5ef38d544647514a9ed7cdb2e3de44d0282d29` |
| condition-conflict: `references/b.md` | 71 | `7c1bac73bb225e2300ee0650c9b4ea38d10800f067c5956c793c760b1ea108d6` |

## Test inventory and execution

P3 adds **40 tests**, without changing any of the 306 existing tests. Coverage includes:

- Exact tenth/twentieth committed trigger; no-change, rejected, P1 failure and frozen
  evaluation exclusions; per-run counters; five unchanged source snapshots for each task.
- Full/unit duplicate detection; bounded BM25; exact merge/dedup and conservative partial
  duplicate rejection; compatible versus different conditions; review veto and keep decisions.
- P1 whole-topic splitting, overflow split/rejection, exact coverage and metadata/provenance
  union, no YAML aliases, hashes/unique IDs and BM25 retrieval after reorganization.
- Root/reference link rewrites, immutable/code/bare-reference safeguards, malformed paths,
  hashes, spans, missing coverage and invented target content.
- Input/call/modified-file budgets, malformed model JSON, timeout, index runtime failure,
  savepoint write failure and final-version disk failure.
- Interrupt before consolidation, during planning, after validation/precommit, and after
  checkpoint/before publication; archived attempts, replay without model calls, corrupted
  counter/artifact rejection and unchanged original versions.

Command: `python -m unittest discover -s tests -q` using
`D:/RSI/.venv-py314/Scripts/python.exe`, `PYTHONUTF8=1` and
`LITELLM_LOCAL_MODEL_COST_MAP=True`. No API credentials or real inference are exercised.

Initial targeted tests exposed shared Python provenance objects producing YAML aliases;
fixed by independent JSON copies. An integration fixture also needed the existing
`execution_mode=single` setting when selecting one frozen rollout.
The first full run (344 tests at that point) hit Windows `WinError 5` replacing a temporary
`latest.json` during publication. No test was removed or softened. A clean full rerun
passed 346 tests (345 passed, 1 skipped) in 166.786s. Final conflict-decision receipt
assertions were then included in one final complete rerun; its authoritative result follows.

**Final result: 346 tests, 345 passed, 0 failed, 1 skipped, 126.334 seconds.**

P3: 40/40 passed. Existing baseline: 305/306 passed, 1 existing Windows
symlink-permission skip. Final log: `D:/RSI-core/_work/p3-tests/full-suite-verified.log`.

Old regression coverage includes P0 transactions, P1 migration, P2 retrieval/shared
context, P2.1 strict/estimated separation, unchanged Trace2Skill and ReasoningBank
paths, schema 5/6 recovery and formal SWE scoring. Vendor and old tests have no diff.

## Files and limitations

New implementation: `rsi/skills/consolidation.py`, `rsi/skills/consolidation_state.py`.
Integration: `rsi/agent/runner.py`, `rsi/experiment/checkpoints.py`, `rsi/core/config.py`,
`rsi/skills/directory.py`; P1 splitter receives optional explicit source selection in
`rsi/skills/migration.py`. Added `tests/test_reference_consolidation.py`, this report,
`docs/skill_routing_p3.md`, the example JSON, and a README phase entry.

Unverified: real-model plan/reviewer quality and billing, real long-running interruption
latency, semantic benefit or downstream task accuracy. No claim of improved correctness
is made. Exact whole-file preservation and strict scope checks deliberately reject many
potentially useful reorganizations. Partial duplicate deletion and semantic rewriting
are not implemented; conservative retention is the fallback. Provider-error usage may
be unavailable and is logged as unknown. The existing Windows publication interruption
path remains recoverable rather than silently treated as a successful commit publication.

All defaults, plan fields, budget limits, code/Skill rollback distinction and P4 reuse
points are detailed in [P3 implementation](skill_routing_p3.md). P4 has not started.
