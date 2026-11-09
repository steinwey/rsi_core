# P1.1 Semantic Experience Unit Migration

One migration algorithm, two trigger modes.

## Shared algorithm

1. **Experience Unit Parser** (`markdown_units.parse_experience_units`): each heading's own body is split
   into top-level list items (`-`, `1.`, `7a.`) and paragraphs. Nested lists, continuation lines, fences
   and indented code stay inside the block that introduces them. Level-1 headings no longer protect
   everything below them. The program keeps these units in place: a global marker or a `Protected Core`
   heading, the opening paragraph under a level-1 heading, units that reference fixed tools
   (`scripts/`, `tests/`), statements about "this skill", and program-written navigation lines.
2. **LLM semantic classification** (plan schema 2): every movable unit is classified as
   `core | specific | mixed | uncertain`; only `specific` units may move.
3. **Topic grouping**: one topic uses one target; a new reference never mixes topics; `requires`
   dependencies stay in `SKILL.md` or move to the same target.
   An operation names only `unit_id`, `target`, `topic`, `applies_when`, `requires` and `reason`; the
   program derives the text, span and hashes from the unit. Operations that break a rule are dropped
   one by one and reported (with the reviewer's rejections) in `previous_error`; a missing or invalid
   classification counts as `uncertain`. Mandatory payloads state `chars_to_move`.
   Topics name a problem family, not a single symptom. A new reference must receive at least
   `min_new_reference_chars` (default 1200) of unit text, otherwise all its operations are dropped;
   existing references and overflow siblings are exempt. The payload lists `reference_sizes`.
   The hard limit outranks the minimum: in the last mandatory round, if `SKILL.md` is still above
   the hard limit, the minimum is 0 so an isolated topic gets its own small reference instead of
   being forced into an unrelated one. Structural migration always applies the minimum.
4. **Deterministic application** (`migration.apply_plan`): verbatim move with rebased links. A new
   reference gets the header `# Title` / `Applies when: ...`; overflow goes to `<target>-2.md`.
   Every moved unit is replaced in place by one navigation line that keeps its list marker or number,
   e.g. `12. Specific guidance (<applies_when>): [Title](references/x.md)`.
5. **Knowledge coverage validation**: source span/hash/fingerprint, target span/hash, navigation text
   and the full root/target reconstruction are checked; any other change rejects.
6. **Independent per-operation review**: the reviewer decides each operation separately; only approved
   operations are re-applied and re-validated, and the rejected ones, with their conflicts and
   uncertainties, become `previous_error` for the next planning round. Then **index synchronization**: `build_index` derives title and
   `applies_when` from the program-written header, so the store's managed-index check stays deterministic.
7. **SkillStore transaction**: maintenance never publishes; `store.propose` → gates → optional P3 →
   the single checkpoint commit.

## Mandatory mode

Triggered when `len(SKILL.md) > root_hard_limit_chars` (10000); target `root_target_chars` (8000);
up to `max_migration_rounds`. If the root is still over the hard limit, the candidate is rejected and the
committed version is kept.

## Structural mode

Never triggered by root length. Runs on the candidate after every effective learning update:

- **Light check** (`structural.light_check`, no model call): changed units, by fingerprint versus the
  base, that are not yet reviewed are scored by code-shaped identifiers (inline code, dotted names,
  snake/camel case, calls, `*Error`), shared identifiers with other root units, and BM25 overlap with
  the reference index. Pending units from an earlier failed review also trigger it.
- **Full review** every `structural_review_interval` (default 5) committed effective updates, over all
  root units, with no keyword or length precondition.
- Light trigger plus a due full review gives one full review. When mandatory and structural coincide,
  only mandatory runs, and its classification counts as the review.
- An empty plan leaves the Skill unchanged. Any structural failure (invalid plan/JSON, review,
  timeout, model/API error) keeps the already valid learning candidate; light candidates are retried
  once at the next effective update.

## Settings

```yaml
skill_maintenance:
  enabled: true
  structural_migration: true        # default when materialized by load_config
  structural_review_interval: 5     # 1..1000
  min_new_reference_chars: 1200     # 0 disables; below max_reference_chars
```

Saved configs that lack `structural_migration` (legacy P1 runs) keep structural migration inactive,
so resumes stay deterministic.

## Ordering and checkpoints

Runner: structural request from `checkpoint.structural_migration` → `maintain_candidate(..., structural=request)`
→ propose/gate → `structural.advance` → P3 consolidation on the migrated learning savepoint → commit.
When both fire, as on the 10th update, migration runs first and consolidation runs on the migrated candidate,
within the same candidate transaction. `commit` and `resume_request` replay `structural.verify_entry` against the
maintenance receipt (`validation.json['structural']`) and reject any counter or receipt mismatch.
