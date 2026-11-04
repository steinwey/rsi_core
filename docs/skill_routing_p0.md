# P0: managed Skill directories and transactions

P0 is storage infrastructure only. It does not migrate prose, retrieve documents,
consolidate references, count autonomous reads, or run new experiments.

## Configuration and compatibility

Existing configurations continue to use directory schema 0 and checkpoint schema 5.
The following three defaults are false; P0 rejects `enabled: true` because those
algorithms belong to later stages:

```yaml
skill_maintenance: {enabled: false}
reference_maintenance: {enabled: false}
skill_routing: {enabled: false}
```

A **new Trace2Skill run** can opt into P0 independently:

```yaml
skill_directory: {schema: 1}
```

This fourth configuration block avoids pretending that enabling migration or routing
already implements those algorithms. ReasoningBank supports schema 0 only. New
configuration defaults are not inserted into saved historical configurations on
resume. Schema 5 is never upgraded; schema 6 cannot be resumed as schema 5.

## Verified integration points

`run_experiment` loads a seed, constructs SkillStore, and initializes/resumes the
checkpoint. `_continue_experiment` reads one current version per task, calls
`run_group`, builds supervision, invokes the selected framework's `evolve_skill`,
then calls `store.propose`, `document_gate`, and `checkpoints.commit`.
`run_group` starts independent `execute_one` workers; `execute_one` installs the
snapshot and injects the unchanged `skill_prompt`. Trace2Skill still performs its
original analysis, MAP, REDUCE, translation, Apply and verification. An instance
adapter guards **all** `apply_edits` batches before writing, including verification
repairs. Its final directory audit rejects changes to fixed tools or managed data.
There are no changes to pinned vendor sources, the Action protocol, Selector,
trajectory truncation, or formal grading.

Future Maintenance belongs between `evolve_skill` and `store.propose`. Future routing
belongs before `run_group` starts its thread pool. Neither is implemented in P0.

## Directory and index contract

`read_skill_directory(..., directory_schema=0)` preserves the `.md/.py` mapping,
UTF-8 universal-newline reading, ordering and old `digest` semantics. The generic
`file_map` is unchanged. Schema 1 additionally reads exactly `routing/index.yaml`;
other YAML and unsupported physical files are rejected. Python cache files remain
excluded. Both modes reject symlinks/junctions, unsafe paths, Windows device names,
case collisions, and file/directory collisions.

Permissions are disjoint: SKILL.md and single-level references/*.md are editor
files; routing/index.yaml is program-managed; all other admitted files are fixed.
The index participates in the complete version digest, not the immutable tools hash.

Schema 1 uses safe YAML with duplicate-key, alias/anchor, size, field and collection
bounds. Paths must exactly cover the references; IDs/paths are unique and each
reference's UTF-8 SHA256 and Python character count must match. Metadata includes
title, description, keywords, applicability, exclusions, provenance and units.
P0 generates conservative metadata and `legacy_unknown`, never success or PASS
claims. Existing metadata is retained only when the document hash is unchanged.
Provenance/unit field shapes are checked on import; P0 does not authenticate external
historical evidence or implement Markdown unit extraction/semantic validation. It
creates no units and no routing consumer uses imported unit declarations.

`build_index(files, previous)` is deterministic. Ordinary proposals cannot edit the
index. `propose(..., managed_changes=..., expected_base_version=...)` offers an
independent trusted interface; even that interface must exactly match the deterministic
builder output, followed by complete candidate validation. No arbitrary LLM metadata
is accepted. P1 can deliberately extend this interface with validated metadata plans.

On new schema 1 runs, `load_initial_skill(..., bootstrap=True)` derives an indexed
seed without modifying the source. `initial-skill-source.json` records source and
derived versions. It does not shorten or normalize legacy document content beyond
the existing reader's newline semantics. Invalid indexed seeds fail closed.

## Transaction and recovery

Versions are written into a unique staging directory beside `versions/<digest>`.
Files are flushed/fsynced, read back, compared to the complete candidate, and validated
before directory rename. Existing destinations are verified, never repaired or
overwritten. Windows sharing violations receive bounded retries. Exceptions clean
only this invocation's staging directory. An abrupt process kill can leave an
unreferenced `.staging-*` directory; it is not a version and cannot become current.
There is no guarantee against hardware loss of an unflushed directory entry.

`propose` does not publish the current pointer. Schema 6 records `directory_schema: 1`
and uses the existing single checkpoint commit point, followed by replayable
`publish`. Commit, publication and resume verify referenced complete versions.
Resume skips committed tasks and archives uncommitted attempts, just as schema 5 does.
No counters or speculative routing state have been added.

Docker cp, Harbor upload_dir and fallback all install a canonical temporary snapshot
in managed mode. A remote manifest verifies the exact file set and byte hashes before
the solver starts. Legacy installation follows its existing path. This verifies a
copy; it does **not** provide P4 read-only protection against later container edits.

## Development boundaries and rollback

Stable worktree: `D:/RSI-core` (main).
Development worktree: `D:/RSI-core-skill-routing` (feature/skill-maintenance-routing).
Baseline: `4fd9948d8ac36560025e105aca1430c82d8714b6`.
Tags: `baseline-pre-skill-maintenance`, and `skill-routing-p0` only after acceptance.

Inspect either code version safely in a new, non-existing worktree:

```powershell
git -C D:/RSI-core worktree add --detach D:/RSI-core-baseline-review baseline-pre-skill-maintenance
git -C D:/RSI-core worktree add --detach D:/RSI-core-p0-review skill-routing-p0
```

These commands preserve both current branches and all working files. To return an
existing clean development worktree to a tagged version without rewriting history:

```powershell
git -C D:/RSI-core-skill-routing switch --detach baseline-pre-skill-maintenance
# Or:
git -C D:/RSI-core-skill-routing switch --detach skill-routing-p0
# Return to development:
git -C D:/RSI-core-skill-routing switch feature/skill-maintenance-routing
```

Git versions source code. SkillStore versions runtime documents and Checkpoint commits
experiment progress. Switching code does not revert Skill state. Baseline code must
not resume schema 6 runs; keep those runs for the P0-capable code. No historical run,
Skill, manifest, configuration or credential was modified by this development.
