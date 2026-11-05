# P4 validation report (2026-11-05)

Worktree: `D:/RSI-core-skill-routing`; branch: `feature/skill-maintenance-routing`.
Parent: `d0b043341251503b41be1d0b8da90c39783c7753` / `skill-routing-p3`.
P4 is committed and tagged locally after this validation as `skill-routing-p4`.
Obtain its exact commit with `git rev-parse skill-routing-p4^{commit}`.
No main merge, remote push, historical tag modification, vendor edit or paid API call.

## Results

**383 tests: 382 passed, 0 failed, 1 skipped, 187.978 seconds.**
P4 adds **37 tests, all passed**, including two opt-in real container integration tests.
All 346 existing tests are unchanged: 345 pass and the existing Windows symlink-permission
case remains skipped. No old tests were removed or weakened.

Executed:

```powershell
$env:PYTHONUTF8='1'
$env:LITELLM_LOCAL_MODEL_COST_MAP='True'
$env:RSI_TEST_DOCKER='1'
$env:RSI_TEST_DOCKER_IMAGE='sweb.eval.x86_64.pallets__click_lc_0061_d959898d:latest'
$env:RSI_TEST_ARTIFACT_DIR='D:/RSI-core/_work/p4-tests'
D:/RSI/.venv-py314/Scripts/python.exe -m unittest discover -s tests -q
```

Authoritative log: `D:/RSI-core/_work/p4-tests/full-suite-final.log`.
Without the Docker opt-in flag, the two real-container tests explicitly skip rather than
pretend to pass. No model credentials are used by these tests.

P0 transactions, P1 migration, P2 five-route routing, P2.1 strict/estimated, P3 periodic
maintenance/recovery, Trace2Skill, ReasoningBank, schema 5/6 recovery and formal scoring
remain covered by the original regression suite. New P3+P4 integration verifies that a
failed optional consolidation still commits its learning savepoint, verifies both types
of receipts and resumes without additional model calls.

Initial development runs found and corrected an audit-summary syntax error. The first
full run found a Windows Harbor subprocess incompatibility: a Selector event-loop policy
in the full process did not support subprocesses, although standalone Harbor passed.
P4-enabled Harbor now owns an explicit Proactor loop, without altering vendor, global
policy or disabled behavior. Final full regression includes both actual container tests.

## Examples A and D: autonomous lookup and cost

Saved portable data: [Mock example](examples/skill_access_mock.json).
Complete per-route logs: `D:/RSI-core/_work/p4-tests/examples/rollout-0` through `rollout-4`.
The solver and Backend are Mock; counting uses the actual installed pinned offline
`tiktoken==0.14.0` / `o200k_base` proxy from P2.1. This is not a real Luna inference run.

Each route receives the same root guide and exactly two automatically loaded references,
`references/a.md` and `references/b.md`. Its first Bash action reads
`/opt/rsi-skills/references/third.md`, whose body was absent from the initial system
message. The file output matches the snapshot and the event is `content_confirmed`.
All five logs are separate and preserve command order; no solver command was modified.

| Rollout 0 metric | Observed value | Meaning |
|---|---:|---|
| Initial Skill tokens | 466 | Proxy BPE estimate, including P4 guidance |
| Read observation tokens | 24 | Proxy BPE estimate of actual model-facing observation |
| Detected read commands | 1 | Content-corroborated cat, not a complete syscall count |
| Unique corroborated references | 1 | `references/third.md` |
| Solver input / output usage | 20 / 10 | Artificial Mock usage fields, not real billed tokens |
| Router model calls | 0 | Local computation only |

466 + 24 must not be presented as API usage, and neither estimate is added to the 20/10
fixture usage or task metrics. Observation text may be resent in later requests, and
unrecognized accesses remain unknown. A successful submission command is itself recorded
as unclassified, rather than incorrectly proving zero hidden reads.

## Example B: real Docker protection and patch boundary

Saved evidence: [Docker validation](examples/skill_access_docker.json).
Actual Docker Engine reported `29.8.2`; Compose reported `v5.6.0` on this Windows host.
The existing local Click SWE image was used with no model calls or official grading.
RSI's real SWEBackend started a private-copy read-only bind, inspected its mount/security
settings, and obtained EROFS for all five operations: create, overwrite, append/modify,
delete and rename. Installation and final manifest hashes matched. The host SkillStore
snapshot was unchanged.

The task Git tree was initially clean and stayed clean after reading Skill files. The
existing artifact extractor produced exactly one intentional task-file diff for
`.p4-test-evidence.txt`; the patch contained no installed Skill paths. Test containers
were closed through existing backend lifecycle methods.

Skill version: `577cdf3b0cd8d011`. Full manifest SHA256:
`5e03c2519ac8bb4d7473548499706f24b6e9736a63826d57ce14adff118064e0`.

## Example C: real Harbor weak installation

Saved evidence: [Harbor validation](examples/skill_access_harbor.json).
A minimal local Harbor task used the existing `python:3.12-slim` image and the real
TerminalBackend / Harbor Docker lifecycle / `upload_dir`. Installation and reading passed.
The receipt explicitly records `install_mode=copy_audited`, `degraded=true` and
`read_only_enforced=false`. An intentional overwrite succeeded, then the final manifest
verification detected it. The original host Skill was unaffected. No verifier accuracy
or full Terminal-Bench task performance is claimed.

The generic Python-copy adapter was also exercised using temporary filesystem tests.
`require_read_only=true` and `install_mode=read_only` reject these weaker adapters before
solver model invocation. Endpoint hash agreement does not prove absence of transient
writes or reads; the limitation remains in each copy receipt.

## Coverage and limits

New tests cover validated complete installation/index/hash identity, fixed tools,
missing/extra/changed files, symlink/path guards, invalid configurations, disabled-context
identity, core budget overflow, actual estimated counter metadata, five independent reads,
API-versus-observation separation, repeated/multiple-path commands, cat/sed/head/tail/
grep/rg/awk/Python, relative/cd/redirection cases, failed/unrecognized accesses, evidence
hash corruption, optional telemetry failure, integrity failures blocking commit, receipt
corruption/config mismatch on resume, and P3/P4 combined recovery.

Best-effort evidence is not complete access control or syscall auditing. Dynamic shell
expansion, aliases, scripts and transient writes can evade inference. Exact cat output
corroborates returned content, not unforgeable file-read provenance. Docker strong
protection is limited to the verified mount and normal container boundary; other engines,
rootless configurations, remote Docker hosts, Windows containers and malicious host/kernel
conditions were not validated. Real provider token agreement, billed cost, long-run
resource behavior and task accuracy improvements were not tested.

E adds guidance, verified read-only protection or explicit degradation, audit and cost
observability to D. D already has Bash read capability. Turning off audit or index hints
does not create a prohibited-read control group. Future A–E performance comparisons need
matched budgets/tasks and explicit mechanisms; an allow/deny-read ablation needs separate
verified access isolation. No formal experiments were launched.

## Changed files and rollback

New modules: `rsi/skills/access.py`, `rsi/skills/access_audit.py`.
Integration: `rsi/agent/execution.py`, `rsi/agent/runner.py`,
`rsi/benchmarks/backends.py`, `rsi/core/config.py`, `rsi/experiment/checkpoints.py`,
`rsi/skills/budget.py`, `rsi/skills/directory.py`, `rsi/skills/routing.py`.
Tests: `tests/test_skill_access.py`, `tests/test_skill_access_audit.py`.
Documentation: README, P4 implementation/validation reports and three example JSON files.
No changes to action protocol, evidence capture, model API code, formal grader, existing
tests, upstream lock or vendor files.

[Implementation and rollback commands](skill_routing_p4.md) include exact P3 checkout,
feature-branch return and independent-worktree commands. Code checkout does not rewind
SkillStore or checkpoints; P4 runs should resume using P4 code and the original config.
Local commit/tag remain unpushed pending separate authorization.
