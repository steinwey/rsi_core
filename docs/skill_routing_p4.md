# P4 — Autonomous Skill Access (2026-11-05)

P4 adds explicit lookup guidance, verified installation protection and best-effort
access observability. P2/P3 already installed the full library and allowed Bash reads.
P4 does not introduce a new LLM tool or change the solver/action protocol, formal grader,
learning algorithm, P3 schedule, or strict/estimated tokenizer semantics. No paid API
experiment or benchmark training/evaluation run was started for this implementation.

## Configuration and compatibility

Enable only in a new managed Trace2Skill run with P2 routing enabled:

```yaml
skill_directory: {schema: 1}
skill_access:
  enabled: true
  install_mode: auto
  require_read_only: false
  expose_index: true
  audit_reads: true
  record_observation_cost: true
  verify_snapshot_before: true
  verify_snapshot_after: true
```

`skill_routing` must retain its existing valid counter configuration. For the three
unverified API aliases, use the explicit P2.1 estimated configuration for exploratory
use; strict still fails safely. P4 does not automatically downgrade counters or provide
new model mappings. P1/P3 may be enabled independently with their existing prerequisites.

Defaults above apply except `enabled: false`. Missing/disabled P4 leaves old installation,
prompt text, route artifacts, API budgets and schema 5/6 behavior unchanged. Existing
ReasoningBank loading remains unchanged; enabled P4 requires managed Trace2Skill.
No existing agent YAML is silently enabled. Unknown keys, invalid modes, non-boolean flags
and incompatible settings are rejected. Before/after verification are deliberately
mandatory when enabled; setting either false is rejected. `require_read_only: true`
cannot be combined with `copy_audited`. `expose_index: false` removes explicit index
instructions, not filesystem access. `audit_reads: false` makes read counts unknown.
Resume still requires exactly the saved configuration; do not enable P4 in an old run.

## Snapshot installation

`access.Snapshot` reuses `load_initial_skill` to verify the version, managed index,
reference membership and immutable builtin tools. It writes only the validated canonical
files to a private, temporary version directory. Neither the original SkillStore nor
project/configuration/checkpoint/log directories are mounted or uploaded. The complete
snapshot includes root guide, references, index, scripts and fixed checks.

A per-file UTF-8 SHA256 manifest and an SHA256 of its sorted compact JSON are recorded.
The manifest digest is named `snapshot_sha256`; it is distinct from the existing
16-character Skill version digest. Installer commands run outside the solver's recorded
Bash actions and are not miscounted as autonomous reads.

| Backend / mode | Installation | Protection |
|---|---|---|
| SWE / SWE-chain Docker, auto or read_only | Startup bind mount of private copy at `/opt/rsi-skills`, `readonly` | Verified mount plus write-denial probes |
| Docker, copy_audited | Existing canonical `docker cp` path | Writable isolated copy, endpoint integrity audit |
| Harbor | Existing `HarborSyncEnvironment.install_skill` / `upload_dir` | Writable isolated copy, explicit degradation |
| Generic compatible Backend | Existing adapter or Python copy installer | Writable isolated copy, explicit degradation |

Docker startup is adjusted in RSI's `SWEBackend` integration, without vendor changes.
The container retains network isolation, drops capabilities (`--cap-drop=ALL`) and uses
`no-new-privileges`. These extra restrictions may affect tasks needing privileged
operations; task accuracy under this configuration was not evaluated. Windows absolute
paths are passed as subprocess arguments rather than interpolated shell commands.
Comma/newline bind source paths are rejected. Docker Desktop path handling was exercised
with the private Windows snapshot directory and a local Linux SWE image.

Before claiming `read_only_enforced=true`, host-side Docker inspection must show exactly
one read-only bind at the target and the required security settings. In-container probes
must receive EROFS for create, overwrite, append/modify, unlink and rename. Probes target
only the disposable copy, never the published Skill. chmod is not used as a substitute.
Auto selects this mount path for the built-in SWE backends; a mount/verification failure
aborts instead of silently retrying with a weaker copy. Explicit copy mode is available.
Other backends cannot claim strong protection merely by copying successfully. Required
read-only mode fails before a model is called if that capability is unavailable.

After installation, a fresh remote manifest must match every file; symlinks, unexpected
files, missing/changed references and non-regular file entries are rejected. Index bytes
matching the host-validated index bind membership and metadata to the snapshot. Hashes
are verified again before extracting the task artifact and at rollout finalization.
Mismatch clears the artifact and fails the rollout, preventing grading/learning/commit.
A host installation receipt failure is an infrastructure error, not telemetry success.
Backend closure precedes private-copy cleanup, so Docker never loses its bind source early.

Writable-copy endpoint checks cannot prove that no transient write, write-then-restore,
background access or forged response occurred. Strong mount protection concerns the mounted
snapshot within the normal unprivileged container threat model, not a compromised host,
Docker administrator or kernel escape. Neither mode is a general OS access trace.

For P4-enabled Windows Harbor, RSI explicitly creates a Proactor event loop so subprocesses
work even if a model dependency has installed a Selector policy. This does not change the
process-wide policy or legacy disabled behavior. Real Harbor upload/lifecycle was tested.

## Initial context and autonomous lookup

The P2 renderer adds a short instruction to inspect the managed index and relevant
references only when loaded guidance is insufficient, using ordinary Bash. The existing
list of automatically loaded references remains visible. Index contents and the whole
library are not redundantly injected. Scripts and core guidance remain present.

The new instruction is included in **every** trial/final P2 token count. Core overflow
still fails; whole-reference selection and P2.1 safety margins remain intact. Routing
runs once before the pool, and all five solvers receive the same frozen context. Each
solver may independently read files not selected initially. Later observations consume
the normal solver context/step/time/cost budget; the initial 4000-token configuration is
not a cap on all future observations or the full conversation.

## Audit evidence and limitations

`access_audit.audit` consumes existing `RecordingEnvironment` command records and raw
outputs plus the actual final model-facing messages. It does not execute, rewrite,
block or add to the agent's commands. Logs are separate for each rollout:

- `rollout-N/skill-install.json`: installation mode, manifest, protection and final check.
- `rollout-N/skill-access.jsonl`: initial injection event, then ordered command events.
- `rollout-N/skill-access-summary.json`: per-rollout access/usage summary.
- Task `skill-access-summary.json`: rollout summaries and checkpoint-linked receipt hashes.

Events bind command sequence, assistant/observation message offsets, command record and
output SHA256, status/error, version, route ID, candidate paths and counter identity.
One event contains all candidate paths for a command so one observation is counted once.

The recognizer considers cat, sed, head, tail, grep, rg, awk, Python literals, cwd-relative
paths, simple cd prefixes, redirections and command combinations. It distinguishes:

- **content_confirmed:** simple single-file cat, successful status, nonempty output exactly
  matches immutable source bytes. This corroborates returned file content; it is not proof
  of an OS read syscall or an unspoofable shell command.
- **inferred:** a reader-like command names candidate paths and the whole command succeeds.
  Conditional branches, mixed output and aliases may mean the particular read never ran.
- **failed_or_unknown / unclassified_command:** failure, dynamic access, unrecognized shell
  syntax or a path mention without a recognized read. These do not prove zero reads.

`autonomous_read_events` is a detected command count, not a complete filesystem-read
count. `unique_references_read` contains only content-corroborated paths; inferred paths
are separate. No detected reads yields unknown (`null`), with observed counts available
separately. Variables, eval, functions, subprocesses, symlink aliases, encoded paths,
heredocs and arbitrary scripts can escape recognition. Empty-file output alone is not
content confirmation. A byte-identical synthetic output can still fool content corroboration.
No claim that all accesses are recorded is made.

Telemetry failures produce `status=unknown` and do not fail an otherwise valid solver.
Security integrity failures do fail it. Raw commands and model messages remain in their
original evidence artifacts. Completed task checkpoints hash the install/audit receipts;
resume verifies their identities and final installation status without executing reads,
models or installers again. Original schema 5/6 and P3 savepoint rules remain intact.

## Token and cost accounting

Metrics are separate and **must not be summed**:

1. `initial_skill_tokens` and, for proxy mode, `initial_skill_tokens_estimated`: the single
   initial SkillContext text count and its counter metadata.
2. `autonomous_observation_tokens_estimated`: counts actual model-facing observation text
   associated with detected candidate commands, once per observation. Mixed command output
   and wrappers are included; exact per-file attribution is unavailable. Unknown counters
   or missing observations produce null. These are local text estimates, not API usage.
3. `solver_input_tokens_api` / `solver_output_tokens_api`: existing solver-reported usage,
   excluding the action formatter, selector and learning roles. Missing usage remains
   unknown; the example clients report artificial fixture usage.
4. Router: zero model calls; existing route `seconds` measures local retrieval/counting
   and I/O. This is not a provider charge.

An observation may be resent in several later API requests. The complete API inputs also
contain task, history, commands and framing. P4 never adds observation counts to existing
report/metrics token totals; no double charging is introduced. Counter identity retains
P2.1's unverified-target and unbounded-error statements. No real API reconciliation or
monetary pricing was performed.

## Patch boundary, ablations and rollback

Snapshot and telemetry live outside `/testbed`. The original artifact extractor and
formal SWE grader are unchanged. Real Docker tests verify an initially clean Git tree,
unchanged status after reads, and a patch containing only one intentional task file.
This does not prevent a model deliberately copying guidance into source; no blanket patch
filter is added to hide such behavior.

D already has Bash access to the full library. E adds explicit lookup guidance, verified
protection/degradation and audit/cost observability. Audit itself is not evidence of
improved correctness. A/B/C/D/E labels alone do not establish causal comparisons. A true
allowed-read versus prohibited-read ablation needs verified filesystem/tool access control;
P4's `audit_reads=false` and `expose_index=false` do not prohibit reads. No such experiment
or formal large-scale run was launched here.

From a clean worktree, inspect P3 or return to P4's feature branch:

```powershell
git -C D:/RSI-core-skill-routing switch --detach skill-routing-p3
git -C D:/RSI-core-skill-routing switch feature/skill-maintenance-routing
```

An independent view that leaves the current checkout untouched:

```powershell
git -C D:/RSI-core worktree add --detach D:/RSI-core-p3-review skill-routing-p3
```

Git checkout does not rewind SkillStore/checkpoints. Resume a P4 run using P4 code and
its exact saved config. To reuse an older Skill, create a new compatible experiment from
that immutable version. Do not edit old checkpoints or reset runtime pointers manually.
P4's local commit/tag are not pushed automatically. After separate authorization:

```powershell
git -C D:/RSI-core-skill-routing push origin feature/skill-maintenance-routing
git -C D:/RSI-core-skill-routing push origin skill-routing-p4
```
