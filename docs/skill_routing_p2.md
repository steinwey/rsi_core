# P2 — BM25 Skill Routing

P2 is off by default. It adds pre-solve BM25 retrieval, whole-file budget selection,
and one immutable initial SkillContext shared by all rollouts. No P3 consolidation,
P4 read audit/read-only installation, remote reranker or paid counting call is added.
P0/P1 index schema, migration, SkillStore and single checkpoint commit point are retained.

## Configuration

```yaml
skill_directory:
  schema: 1
skill_routing:
  enabled: false
  retrieval_method: bm25
  top_k: 2
  max_initial_skill_tokens: 4000
  allow_zero_matches: true
  budget_mode: strict
  bm25:
    k1: 1.2
    b: 0.75
```

Enabled routing requires managed schema 1 and Trace2Skill. Maintenance is independent.
ReasoningBank keeps schema 0 and its original injection. Unknown keys and wrong types
are rejected: booleans are not numbers; top_k is integer 1..1000; budget is integer
1..1000000; k1 is finite and 0 < k1 <= 100; b is finite and 0 <= b <= 1. Only bm25 and
strict are supported. load_config expands enabled defaults into the saved config.
Historical raw configurations are not upgraded; resume requires exact equality.

## Retrieval

run_group routes before framework preparation and ThreadPoolExecutor. Only raw task ID
and task["prompt"] are passed. The ID is logged, never scored; task_hash is prompt UTF-8
SHA256. No golden patch, scores, trajectories, labels or future feedback enter retrieval.
The full snapshot is read with P0 policy, index hashes/path coverage are validated and
the full digest must equal the version folder name. Routing never edits a snapshot.

retrieve_bm25 consumes a validated snapshot/index. Title, description, keywords,
applies_when and body are concatenated with equal weight 1. exclude_when contributes
no positive evidence: it filters contiguous case-insensitive word/identifier phrases,
with punctuation boundaries and CJK character sequences. This is lexical filtering,
not semantic rule evaluation. applies_when contributes evidence, not a guessed hard rule.

identifier-cjk-v1 retains lowercase full identifiers plus snake_case, CamelCase,
acronym/error-name and dotted-module components. Components are deduplicated per lexeme;
repeated original words retain term frequency. CJK uses overlapping bigrams, retaining
single characters. The small fixed stopword set is versioned in source. No online
segmentation, stemming, embeddings or synonyms are used.

Standard BM25 uses unique sorted query terms, IDF log(1+(N-df+.5)/(df+.5)), and the
standard term-frequency/length normalization. All documents define corpus statistics,
including subsequently filtered files. Sort by score descending then path ascending.
Only positive applicable candidates proceed. Never fill Top-K with unrelated files.
Scores are relevance values, not success probabilities or formal grades.

## Token capability and budget

TokenCounter is a protocol with CounterInfo(model, identity, accuracy, statement) and
count(text). Model mismatch, estimated precision, broken counters and invalid counts
fail closed. The default factory has no network dependency and never guesses aliases.

| Model | Bundled strict support | Finding |
|---|---|---|
| gpt-6-luna | Unsupported | Installed tiktoken 0.14.0 has no exact/prefix mapping; no verified offline mapping established. |
| qwen3.8-max | Unsupported | No verified exact offline mapping bundled for this API model. |
| qwen3.8-flash | Unsupported | No verified exact offline mapping bundled for this API model. |
| deepseek-v4.1-flash | Strict, pinned checkpoint tokenizer.json | Counts the published DeepSeek-V4.1-Flash vocabulary. Token Plan usage is not compared. |
| Unknown/provider aliases | Unsupported | No model-name guessing or fallback. |
| Explicit injected test counter | Test only | Synthetic units, explicitly not real model tokens. |

**Enabling strict routing on gpt-6-luna, qwen3.8-max, or qwen3.8-flash fails with
counter_unavailable.** deepseek-v4.1-flash strict routing counts its pinned checkpoint
tokenizer.json. That count is exact for the file, and it has not been compared with
Token Plan usage. The implementation does not claim gateway tokenizer accuracy.
A later reviewed exact adapter can implement the protocol through the Python
counter_factory dependency. That is trusted code, not a config escape hatch.
Test fixtures use accuracy=test_only and one Unicode codepoint per synthetic token;
no production config enables this fixture. No chars/4 approximation is implemented.

Research on 2026-11-04: [OpenAI token counting](https://developers.openai.com/api/docs/guides/token-counting)
and the [Luna model page](https://developers.openai.com/api/docs/models/gpt-6-luna)
did not establish a bundled local Luna vocabulary mapping. The documented remote
counting endpoint is not called. [QwenCloud token counting](https://docs.qwencloud.com/developer-guides/run-and-scale/token-counting)
describes pre-request estimation and response usage; the
[Flash page](https://help.aliyun.com/en/model-studio/qwen3-8-flash) did not provide a
pinned exact local mapping. These are current adapter-capability findings, not a claim
that vendors cannot offer counting services.

render_context assembles the actual final rsi_skill string: full untruncated SKILL.md,
toolkit prose, sorted tools, access instructions, loaded-file list, headings, paths,
separators and complete selected bodies. It does not count the base system template,
user prompt, chat envelope, later reads or total solver usage.

select_context counts core first and rejects overflow. It then tries ranked files,
counting the entire trial context, accepts complete files that fit, skips oversized
files and tries later candidates until top_k files are accepted. Final text is counted
again; fragment counts are not added. Exactly 4000 is accepted, 4001 is not. No document
is truncated. An oversized root fails; it is not automatically migrated by the router.

No applicable positive match yields no_relevant_references; all relevant files too
large yields references_over_budget. Both normally produce core-only. With
allow_zero_matches=false, no positive applicable match fails after counter/core checks;
budget-only skips still permit core-only. Index corruption, version mismatch, core
overflow and unavailable/broken counter are distinct failures. Counter unavailability
is not waived for empty retrieval. Checks precede solver workers; existing fixed-tool
gating and optional P1 bootstrap remain separate earlier initialization operations.

## Integration and artifacts

SkillContext is a frozen dataclass; RouteResult stores immutable JSON and returns a
fresh dictionary when requested. All workers receive the same context object.
execute_one verifies its snapshot/hash identity and uses the existing
agent.run(task["prompt"], rsi_skill=guide) injection. It does not retrieve or assemble.
When disabled it calls the unchanged skill_prompt, with no router, counter or assembler.
Selector, formal grading, action protocol and upstream mini-SWE source are unchanged.

Full snapshots, including unselected references and index, still install through P0
manifest verification. Unselected bodies are not injected. Bash access remains available;
the context names loaded files and the location of the remaining library. No new read
restriction, audit counter or read-only install is introduced.

Each routed task writes skill-route.json and, on success, exact UTF-8 skill-context.txt.
Train/test share one schema. Records include scores, exclusions, budget trials,
configuration, algorithm/tokenization versions, counter identity/accuracy, hashes and
time. Failed routes record explicit reasons rather than successful zero matches.
Route ID hashes canonical sorted JSON with route_id=null and seconds excluded; there
are no random IDs, timestamps or absolute paths in the identity. Python/libm floating
point is deterministic for the pinned runtime, not a cross-platform last-bit guarantee.

Rollouts archive route/context/index identities. Task summary and checkpoint link the
route-file SHA256. Commit/resume verify task, config, Skill/index, context and route ID.
Schema remains 5/6. Committed tasks resume without rerouting or recounting; incomplete
attempts are archived before retry from saved state. Publish remains idempotent.
Receipts detect corruption, not attackers able to rewrite both artifacts and checkpoint.

Reranker is a minimal Protocol only. P3 can consume retrieve_bm25 with validated
snapshots for neighbor candidates, but must implement its own bounded maintenance
policy. No consolidation is implemented here.

## Safe rollback

From a clean development worktree:

```powershell
git -C D:/RSI-core-skill-routing switch --detach skill-routing-p1
git -C D:/RSI-core-skill-routing switch feature/skill-maintenance-routing
```

At unused paths, inspect both versions simultaneously:

```powershell
git -C D:/RSI-core worktree add --detach D:/RSI-core-p1-review skill-routing-p1
git -C D:/RSI-core worktree add --detach D:/RSI-core-p2-review skill-routing-p2
```

Code rollback does not roll back SkillStore or Checkpoint state. P1 rejects enabled
routing configs: do not resume P2-enabled runs with P1 code. No reset, clean, force
push, historical tag replacement or main merge is required. Stop until explicit P3.
