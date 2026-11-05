# P3 — Reference Consolidation (2026-11-05)

P3 is optional periodic maintenance for schema 1 Trace2Skill runs. It defaults off.
It preserves P0 immutable snapshots, P1 mandatory validation, P2 routing, P2.1
strict/estimated token semantics, formal scoring and the original learning pipeline.
No Jev, P4 read auditing, paid model experiment or historical Skill rewrite is included.

## Placement and transaction boundary

`runner._continue_experiment` runs the existing Trace2Skill proposal, P1 mandatory
maintenance, isolated `SkillStore.propose` and document gate first. An accepted,
changed candidate is a **learning savepoint**. P3 can consolidate this complete map;
program-validated results use the existing `SkillStore.materialize`, followed by the
complete document gate. Materialization never changes the published pointer.

A schema 6 checkpoint stores `reference_maintenance` with `accepted_updates`, `checks`
and a round-robin `cursor`. The candidate for update 10, 20, etc. triggers maintenance
before commit. Its tentative counter transition is saved in the task update, but becomes
effective only when the task checkpoint is atomically written. Each new run starts at
zero; imported Skill provenance is not an imported experiment counter.

No-change, unchanged candidates, rejected proposals, mandatory maintenance failures,
bootstrap, fixed evaluation, incomplete tasks and replay do not increment this counter.
Optional maintenance itself is not a learning update. A failed/skipped check consumes
that scheduled opportunity only when the learning update commits; the next is at 20,
not an immediate retry on update 11.

`learning_skill_version` identifies the mandatory savepoint. P1 receipts remain bound
to that version, even when P3 produces a different final `after_skill_version`.

## Bounded candidate discovery

`consolidation.discover` is a separate file-to-file interface; it does not modify the
solver router. It indexes complete-file SHA256 and P1 unit hashes including inherited
context, then uses P2 `retrieve_bm25` for at most K anchor queries against the managed
index. Default K=8. Duplicate buckets select bounded representatives, not every pair.
A rotating first anchor prevents unchanged files from being permanently starved;
changed references and other sorted paths fill remaining anchor slots.

The returned candidate set, pair kind, BM25 score and cursor are saved. Hash matches
are candidates, never deletion authority. Local corpus reading is bounded by existing
P0 directory/index limits; retrieval is O(K*N), not unbounded O(N²) comparison. Only
selected documents/descriptors and bounded split recipes enter the planning prompt.
Independent review receives changed documents and coverage, subject to the same
cumulative input-character budget. The unchanged complete library is not sent to the LLM.

## Plan, coverage and conservative operations

One structured plan per round contains `schema`, `input_hash`, `operation`, `sources`,
`targets`, `coverage`, `reference_changes`, `reason`. Every source descriptor contains
path, full SHA256/span, P1 unit IDs/spans/hashes, applicability and exclusions. Target
contents and paths are exact values, not shell instructions. The LLM cannot write files.

- **MERGE:** at least two files, identical explicit index conditions/exclusions and
  extracted P1 applicability. A new reference must equal the sorted complete original
  bodies joined with one newline, with P1 relative-link rebasing only. Duplicate heading
  anchors are conservatively rejected. No summarization or semantic rewriting.
- **DEDUPLICATE:** complete body and conditions must be identical. Retain an existing
  source, remove only proven whole-file duplicates, and union source provenance.
  Partial duplicate detection is supported; partial deletion is deliberately not authorized.
- **SPLIT:** reuse P1 `split_references(..., sources=[...])` and its exact source/target
  reconstruction with `validate_coverage`. Complete scoped leaf sections retain ancestor
  context; the original file retains heading/navigation stubs. Without `sources`, P1's
  previous oversized-only behavior is unchanged.
- **KEEP_SEPARATE / CONFLICT_UNRESOLVED:** no writes. Conditional disagreement, unique
  exceptions, unprovable coverage or uncertain dependencies cannot authorize deletion.
  A proposed merge with incompatible conditions is rejected; the planner may explicitly
  mark `CONFLICT_UNRESOLVED` on retry. Conflict rejection remains in the attempt log.

Every successful changed plan needs a separate editor-model review with no conflicts
or uncertainties. This is an additional veto, **not formal semantic equivalence**. No
review can override deterministic content/condition checks. Same-text assumptions with
unstated environmental differences remain a limitation; real usefulness is untested.

For merge/dedup, coverage records exact complete source and target spans/hashes; source
unit descriptors account for every original unit within those complete files. Primary
records describe the pre-link-rewrite stage, whose target hashes are also recorded.
Deterministic link rewrites and optional overflow splitting have separate ledgers. Resume
replays the plan and requires exact final files and coverage (including review) to match.

References remain at most 5000 canonical Unicode characters. Overflow after merge is
passed through P1 whole-topic splitting; if any topic/context cannot fit, abandon the
optional batch. No character truncation occurs. Root changes from link repair must also
pass P1 limits. Ambiguous bare/code/HTML-style or absolute references are rejected;
immutable files mentioning a removed reference also veto the move. Supported Markdown
links are repaired in root and references, with query/fragment retained. Existing complete
consistency checks reject dangling local links. This is intentionally conservative rather
than a general Markdown/HTML URL rewriting engine.

## Controlled index and integrity

The LLM cannot supply `routing/index.yaml`. The program uses P0 `build_index`, then
preserves the exact deduplicated union of keywords, applicability, exclusions and
provenance from original entries. Target titles/descriptions and IDs are deterministic;
hashes/character counts reflect final bodies. Source units are retained in the coverage
ledger; new index entries do not pretend old offsets are final offsets. Unknown provenance
remains unknown; no PASS evidence is created. Independent JSON copies avoid YAML alias
emission when several split files share provenance. P0 index validation enforces limits,
unique IDs and exact membership. P2 reads the new immutable snapshot normally.

## Failure and recovery

Before optional work, `consolidation/savepoint.json` captures the validated map/version.
Invalid JSON/plans, conflict/uncertainty, coverage/size rejection, ordinary model failures
and deadline/budget exhaustion return this exact map with `consolidation_failed` or
`consolidation_skipped`. The accepted learning update is then committed normally.

Disk errors, invalid existing versions, unexpected index-generator failures and final
materialization/gate failures abort the task. They are not downgraded into optional
success. KeyboardInterrupt also propagates. No published version is overwritten.

`consolidation_state.verify_entry` verifies artifact SHA256, savepoint version, output
version, deterministic plan replay and coverage/review. Resume reconstructs the counter
from ordered committed entries and compares it with checkpoint state. Completed tasks
replay publication without further model calls. Uncommitted task folders are archived
by existing `archive_incomplete`; resumed tasks re-execute rather than trusting residual
plans. This can spend additional calls for an interrupted uncommitted attempt; the old
attempt artifacts remain available and the counter never double-increments.

## Configuration and cost limits

Add to a **new copy** of an existing Trace2Skill agent configuration:

```yaml
skill_directory: {schema: 1}
skill_maintenance: {enabled: true, max_reference_chars: 5000}
reference_maintenance:
  enabled: true
  interval_accepted_updates: 10
  max_candidate_files: 8
  candidate_neighbors: 5
  max_candidate_pairs: 8
  max_llm_calls: 6
  max_rounds: 3
  timeout_seconds: 300
  max_reference_chars: 5000
  max_input_chars: 120000
  max_output_tokens: 8192
  max_output_chars: 100000
  max_modified_files: 16
```

P1 must be enabled with the same reference limit. Integers reject booleans, nonpositive
values and excessive upper bounds; unknown keys are rejected. Schema 1 still requires
Trace2Skill. P3 is independent of whether P2 routing is enabled.

Each call reuses `skill_editor_model`, disables internal retries, and caps output at the
smaller of editor `max_tokens` and P3's cap. At most three plan/review rounds, six calls,
120000 cumulative serialized message-content characters and sixteen changed/deleted/new
files including the managed index are allowed. Each output also has a character cap.
The wall-clock deadline is forwarded to the existing model timeout implementation and
checked between stages. It cannot preempt arbitrary external code that ignores deadlines.
Default maximum requested output is 6*8192=49152 tokens; actual editor settings can lower
it. The input bound is explicitly **characters**, not verified provider tokens or a USD cap.

`usage.json` records model-reported input/output/reasoning tokens, cost, attempted calls,
input characters, per-call events and rejected rounds. Missing usage on failed requests
is marked `usage_known: false`; reported totals cannot be assumed to cover unknown billed
work. Task reports include known consolidation tokens separately and in aggregate.
Request/response/error JSON and stream logs retain attempt detail. No real API usage or
provider price was validated in P3. Mock counts are fixture values, not cost forecasts.

For an exploratory Luna+Max configuration, copy
`configs/agent/routing-estimated-gpt6-luna.yaml` to a new file, retain its explicit proxy
counter identity/asset/safety margin and add the block above. This does not make strict
counting available for aliases. Start only after explicit experiment authorization using
the existing CLI with the new config and a new run output. No existing config is enabled
or historical experiment resumed with changed settings by this change.

## Recovery commands and P4 handoff

Code-only inspection of the P2.1 baseline, from a clean worktree:

```powershell
git -C D:/RSI-core-skill-routing switch --detach skill-routing-p2-tokenizer
git -C D:/RSI-core-skill-routing switch feature/skill-maintenance-routing
```

Alternatively create an independent inspection worktree:

```powershell
git -C D:/RSI-core worktree add --detach D:/RSI-core-p21-review skill-routing-p2-tokenizer
```

A code checkout does not rewind runtime Skill versions or checkpoints. Do not edit an
existing checkpoint/config or delete immutable versions to force a downgrade. To reuse
an older Skill, start a new experiment pointing `initial_skill` at that complete version,
with compatible configuration. Resume a P3 checkpoint with P3 code; P2.1 does not verify
P3 receipts and should not be used to continue that run.

P4 may reuse full-directory install/manifests in `rsi/benchmarks/backends.py`, P0 index
validation and content hashes, P2 routing receipts/shared SkillContext, existing rollout
recording and command traces, and P3 provenance/coverage ledgers. These are handoff points,
not a new autonomous-read audit implementation. P4 has not started.
