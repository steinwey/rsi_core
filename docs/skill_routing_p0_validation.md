# P0 acceptance report — 2026-11-04

## Scope and Git

- Baseline: `4fd9948d8ac36560025e105aca1430c82d8714b6`; local and remote main agreed before development.
- Stable worktree: `D:/RSI-core`, branch main, retained unchanged.
- Development: `D:/RSI-core-skill-routing`, branch feature/skill-maintenance-routing.
- Baseline tag: baseline-pre-skill-maintenance. Accepted code is identified by skill-routing-p0.
- No P1–P4 modules, real model calls, or benchmark experiments were started.

## Test execution

```powershell
Set-Location D:/RSI-core-skill-routing
$env:TEMP='D:/RSI-core/_work/p0-tests'
$env:TMP=$env:TEMP
$env:PYTHONUTF8='1'
$env:LITELLM_LOCAL_MODEL_COST_MAP='True'
& D:/RSI/.venv-py314/Scripts/python.exe -m unittest discover -s tests -q
```

- Original baseline: 155 tests, 155 passed, 0 failed, 0 skipped.
- Final P0: 182 tests, 181 passed, 0 failed/errors, 1 skipped; 21.598 seconds.
- Skipped: creating a real symlink requires privileges unavailable on this Windows host.
  The rejection path is separately covered with injected symlink detection. Real
  Windows junctions and hostile concurrent filesystem mutation were not exercised.
- No dependency-related skips. Initial sandbox-only execution could not create the
  separate worktree's cache; rerunning with authorized worktree access resolved it.
- All original tests are unchanged, including ReasoningBank, Schema 5 recovery,
  failure PASS gate, upstream alignment, Action protocol and five-route concurrency.
- New tests use Mock LLM/Fake Backend and the real pinned Trace2Skill editor. No real
  Docker/Harbor containers or paid APIs were used. Docker cp and Harbor upload are
  adapter tests, not live platform certification.

Full local logs (not committed as generated artifacts):

- `D:/RSI-core/_work/p0-tests/baseline.log`
- `D:/RSI-core/_work/p0-tests/final-suite.log`

Earlier failure logs are retained in the same directory. An installer byte mismatch
found by the fallback test was fixed before the final run; the final managed fallback
writes UTF-8 bytes while legacy fallback retains its original write behavior.

## Covered acceptance properties

- Legacy seed hash remains `2e97278ac34006f9`; no changes to generic file_map.
- Index in complete version hash, excluded from tools hash; source seed unchanged.
- Deterministic serialization, exact reference coverage, creation/deletion/rename,
  duplicate IDs/paths/YAML keys, hash/char count mismatch, invalid YAML/aliases/size.
- Unsafe paths, Windows names, case collisions, symlink rejection and illegal FileEdit
  batches rejected before any permitted edit in that batch is applied.
- CRLF/Unicode disk-read and index roundtrip.
- Fixed tools immutable, existing destinations verified rather than overwritten.
- Staging write failure, directory publication failure and Windows sharing retry.
- Crash before checkpoint, crash after checkpoint before publication, repeat resume
  with no repeated model calls or duplicate task commits; schema mismatch rejection.
- Real upstream reference creation followed by managed index sync and inheritance
  of the same Skill/tool hashes by all five routes on the next task.
- Canonical installation through fallback, Docker cp and Harbor upload; rejection of
  missing index or unexpected installed files.
- Disabled features and ReasoningBank retain legacy mode. Unsupported future feature
  flags fail explicitly instead of silently claiming to run unimplemented algorithms.

## Diff and protected sources

`git diff --check` passed. The pinned Trace2Skill manifest's file SHA256 values were
verified. No changes to vendor/, built-in Skill, generic file_map, Action protocol,
task manifests, external mini-SWE source, formal grading or Selector. Backends changes
are confined to installation. Historical runs, checkpoints, Skill versions, configs
and credentials are untouched. New development test outputs live only in temporary
folders under `_work/p0-tests` and the separate worktree cache.

## Limits and next-stage dependencies

No known failing P0 test remains. The privileged symlink test and live container
installation remain platform coverage gaps. Staging protects publication from
process-level write failures; abrupt hard kill can leave unreferenced staging files,
and it is not a power-loss durability guarantee. Copied installed Skills are not
read-only (P4).

P0 validates imported provenance/unit shapes, but does not verify historical source
receipts or infer semantic independence of Markdown units. Its builder creates only
legacy_unknown provenance and no units. P1 must add explicit, validated metadata and
coverage interfaces, rather than treating these optional imported declarations as
proof. No migration, consolidation, BM25 or token-budget implementation exists yet.

See `skill_routing_p0.md` for safe worktree rollback commands and the distinction
between Git code tags and runtime SkillStore/checkpoint versions. Stop at P0; P1
requires the user's explicit instruction “继续 P1”.
