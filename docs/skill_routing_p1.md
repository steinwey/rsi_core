# P1 — lossless Skill Migration

This stage adds independent Maintenance after Trace2Skill's completed verification,
before SkillStore.propose. It does not implement solver routing, BM25 routing,
periodic consolidation, autonomous read auditing, or semantic summarization.
P0 code/tag/documents are preserved.

## Enable only for a new run

```yaml
skill_directory:
  schema: 1
skill_maintenance:
  enabled: true                  # default false
  root_hard_limit_chars: 10000
  root_target_chars: 8000
  max_reference_chars: 5000
  max_migration_rounds: 3
  max_llm_calls: 8
  timeout_seconds: 300
reference_maintenance:
  enabled: false
skill_routing:
  enabled: false
```

The reference size setting belongs to `skill_maintenance` in P1: it is a mandatory
per-candidate invariant, independent of future periodic consolidation. All numeric
settings are integers, excluding booleans, and positive. Character limits are bounded
at 1,000,000, rounds at 20, calls at 64, and timeout at 3600 seconds; target must be
strictly below hard limit. Unknown settings are rejected. Enabling Maintenance requires
P0 directory schema 1 and Trace2Skill. The remaining feature flags still reject enabling
unimplemented stages. No token budget or tokenizer claim is introduced.

Enabled config loading expands the defaults into the saved configuration. Historical
saved configs are read without insertion or upgrade. Disabled Maintenance follows the
P0 update path, accepts legacy long documents, and does not create Maintenance logs or
make Maintenance calls. ReasoningBank remains in legacy mode.

## Actual execution

1. After the original upstream proposal and verification, build an isolated dictionary
   from the current complete snapshot plus the authorized editor document changes.
2. Canonicalize only candidate documents: strip an initial BOM, convert CRLF/CR to LF,
   and retain exactly one terminal newline. Do not normalize Unicode or rewrite the
   source archive. Record all changed document hashes.
3. Validate documents and the managed index. Split any oversized reference at safe
   leaf-section boundaries, then independently review its dependencies.
4. If root is above the hard limit, ask the existing skill_editor_model for a bounded
   MigrationPlan. Suggest up to five existing destinations using deterministic lexical
   overlap. This local destination suggestion is not BM25 and never changes solver input.
5. Validate every operation, construct its complete destination, replace its source
   section with the original heading and a navigation link, and verify coverage.
6. Use a separate model request, without the planner conversation, to review conditions,
   dependencies and conflicts against actual before/after documents. Its approval cannot
   override deterministic checks.
7. Repeat within round/call/time limits, preferring the soft target. A verified result
   between target and hard limit is allowed if the bounded process cannot safely go
   further. A result above hard limit is always rejected. An initially compliant root
   is not migrated merely to reach the soft target.
8. Rebuild the index, check all document limits, fixed tools, consistency and the original
   pinned quick_validate frontmatter validator. Return document changes and separately
   managed index changes to P0 SkillStore; do not publish a pointer.
9. SkillStore, document gate and the checkpoint commit path recheck the size invariants.
   The original checkpoint-then-publish ordering remains the sole commit point.

The editor role/model/temperature/output limit are reused. Maintenance disables hidden
transport retries for its own client: each attempted request consumes one of its call
slots, including invalid responses and independent reviews. Other roles' settings are
unchanged. The model receives a deadline; expiry is checked before/after calls and
before acceptance. No unbounded repair loop or semantic rewriting fallback exists.

## Markdown units and Protected Core

`markdown_units.parse_units` records exact character spans, heading paths, SHA256,
raw content, ancestor context, applicability lines and a protected flag. It recognizes
YAML frontmatter, ATX/Setext headings and fenced code. Lists, quotes, tables and code
blocks remain intact within leaf sections; it does not split on every hash character
or a character offset.

Frontmatter, top-level document content, ancestor introductions and recognized generic
core/workflow/validation/tool headings are retained. Explicit protected-core subtrees
and detected unconditional global constraints cannot be selected. A movable leaf also
needs a written conditional applicability line with a technical scope marker. The
program uses conservative English/Chinese patterns; these are guards, not a universal
semantic classifier.

Mixed parent sections keep their general introduction; scoped leaf children can move
with copied ancestor context. An atomic mixed section, an uncertain classification, an
unscoped section or a protected unit is rejected rather than summarized. The model may
only propose `classification: specific` plus `operation: move_verbatim`; other classes
cannot authorize removal. Unsupported or unclosed syntax fails closed.

## MigrationPlan and Knowledge Coverage

Plans include schema, complete input mapping hash and bounded operations. Each operation
contains unit ID, original span/hash, destination path, exact target_content, classification,
an exact source applicability string, coverage IDs and a reason. Ordinary LLM file edits
are never executed. Duplicate JSON keys, invalid fields/paths, duplicate units, stale
hashes/spans, rewriting, missing coverage and no-progress operations are rejected.

The deterministic validator reconstructs the entire source and every destination:

- Every moved span must match the parser's source unit and SHA256.
- Destination content must equal the full source unit plus deterministic ancestor context,
  with only supported Markdown link rebasing. A summary or omitted exception cannot pass.
- Target offsets/hashes must match; all unrelated file content must be unchanged.
- Original headings remain at source as navigation, preserving incoming heading anchors.
- Conditions, actions, exceptions and verification text are retained verbatim. Separate
  semantic review may reject implicit dependencies or conflicts; it cannot bless lost bytes.

Simple inline Markdown paths are rebased relative to the new file. Fragment-only links
are redirected to the original file, whose heading remains. Relative command paths,
shortcut/reference-style links, footnotes, ambiguous inline code examples, HTML blocks,
and phrases such as “see above” are conservatively rejected. This is a supported subset,
not a claim to implement all CommonMark or prove arbitrary semantic independence.

## Reference overflow

Appending to an existing related file retains its bytes. If the resulting reference
would exceed its limit, the complete scoped topic is placed in a deterministic new
sibling file instead. A single topic plus required context above the limit is rejected.

An already oversized reference is split into whole scoped leaf topics. Its original
path retains context and heading/navigation stubs, so existing inbound links do not
become dangling. Every new file includes ancestor prerequisites. Exact coverage and
an independent dependency review must pass. If retained context itself remains too
large, or no safe independent topics exist, reject the entire candidate. No truncation,
summarization, deduplication or periodic consolidation is used.

## Index, provenance and transaction

P0 `build_index`, `validate_index` and the independent `managed_changes` interface
remain authoritative. The index and documents are one atomic content-hashed version.
Trace2Skill still cannot edit routing/index.yaml. Fixed tools and tests remain immutable.

Unchanged reference metadata is preserved. Verifiable append-only extensions retain
existing provenance identities and add legacy_unknown for new material. Newly moved or
split topics use legacy_unknown rather than claiming invented historical PASS. Their
actual source path, spans, unit/context hashes and target spans are retained in the
coverage ledger; input/normalized-input snapshots preserve the original index and
historical provenance, and old Skill versions remain untouched. P1 does not authenticate
legacy external receipts or fabricate unit-level attribution from file-level evidence.

## Artifacts, usage and resume

Each attempted task writes `maintenance/` with input, normalized input, normalization,
per-call request/response/error, coverage, usage and validation JSON. Accepted candidates
also save output.json. Usage reports successful provider usage separately from attempted
calls; task reports include Maintenance input/output tokens on both acceptance and normal
rejection. Failed attempts retain their usage when archived, following existing semantics.

Checkpoint schema remains 6. Maintenance artifact paths and SHA256 receipts are stored
with the task update and checked on commit/resume. Completed tasks do not rerun migration,
reviews or scoring after publication failure. No maintenance counters are introduced.
Schema 5 and P0 schema 6 configurations remain readable without upgrades.

Bootstrap runs the same bounded process before the first checkpoint and records a derived
seed/source identity. Source files are unchanged. Bootstrap usage is stored separately
in bootstrap-maintenance/usage.json and run-info.json, not added to a task's tokens.
A bootstrap failure marks the new run failed and creates no task checkpoint; no tasks
have started, so retry initialization in a new output directory. Established runs use
normal checkpoint resume and archive incomplete task attempts.

Mandatory invalid plans, uncertain coverage, exhausted plan budgets and oversized atomic
content produce MaintenanceRejected (the existing SkillProposalRejected family): keep
current Skill, record rejection, commit completed formal scores. API, deadline, disk and
validator-runtime failures propagate, leave the task uncommitted and preserve logs.

## Interfaces for P2, without implementing P2

- `read_skill_directory` and `validate_index`: complete indexed snapshot and hash checks.
- `parse_units`: conservative spans and heading/dependency context; no promise of general
  semantic independence, and imported P0 index units are not automatically trusted.
- `maintain_candidate`: isolated candidate -> validated document/managed changes and logs.
- `validate_limits`: canonical character invariants shared by Store, gate and recovery.
- Existing `run_group` before its thread pool is still the future once-per-task routing
  integration point. TokenCounter, BM25 solver routing and routing records do not exist yet.

## Git rollback

P0 is `86512bb6d47fa68772bcec561d1d257711b9bbbd`, tag `skill-routing-p0`.
From a clean development worktree, inspect P0 without rewriting the feature branch:

```powershell
git -C D:/RSI-core-skill-routing switch --detach skill-routing-p0
# Return to the latest development commit:
git -C D:/RSI-core-skill-routing switch feature/skill-maintenance-routing
```

Alternatively create a fresh review worktree at an unused path:

```powershell
git -C D:/RSI-core worktree add --detach D:/RSI-core-p0-review skill-routing-p0
```

Tags version code, not runtime Skill/checkpoint state. Do not resume P1-enabled runs
under P0 code, which deliberately rejects the enabled Maintenance configuration.
No reset --hard, clean, force push, main merge or historical experiment change is required.
Stop after P1 acceptance; P2 requires the user's explicit “继续 P2”.
