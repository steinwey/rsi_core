# P2.1 — explicit usable proxy token counting (2026-11-05)

P2.1 adds the user-authorized **option C**, estimated routing, for all three existing
solver names. It does not upgrade any alias to verified strict support. P2 strict
fail-closed behavior, BM25, five-route sharing, assembler and receipts remain intact.
No P3 work, real benchmark launch, credential probe, inference or counting API call
was performed. Only the public static BPE vocabulary was downloaded during setup.

## Provider and tokenizer research

Local configuration and rsi/core/model.py were inspected. Requests use
litellm.completion(model=<configured name>, messages=..., custom_llm_provider=openai)
and thus Chat Completions semantics, with provider-specific options. A provider label
is removed before transmission; it is not evidence of actual model lineage.

| Model | Configured service | Strict | Implemented path | Validation |
|---|---|---|---|---|
| qwen3.8-max | qwen-token-plan; token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1 | No | Explicit pinned o200k proxy estimate | Local BPE and five-route machinery; no usage comparison |
| qwen3.8-flash | Same Token Plan endpoint | No | Explicit pinned o200k proxy estimate | Local BPE and configuration; no usage comparison |
| gpt-6-luna | sub2api; sub2api.47.250.156.179.nip.io/v1 | No | Explicit pinned o200k proxy estimate | Local BPE and five-route machinery; no usage comparison |

The local endpoint and public model strings are known; the deployed server's actual
upstream model/tokenizer and rewrite configuration were NOT inspected or authenticated.
Sub2API supports endpoint-specific aliases and upstream_model rewriting; repository
features do not prove what this particular deployment routes to.

Sources checked:

- [Qwen Max model](https://help.aliyun.com/zh/model-studio/qwen3-8-max),
  [Flash model](https://help.aliyun.com/en/model-studio/qwen3-8-flash),
  [Token Plan overview](https://docs.bailian.console.aliyun.com/zh/model-studio/token-plan-personal-overview):
  identify offered API models but do not establish a pinned local tokenizer equivalent
  to the configured endpoint. Do not substitute an open Qwen model merely by name.
- [QwenCloud token counting](https://docs.qwencloud.com/developer-guides/run-and-scale/token-counting):
  illustrates o200k_base for estimation; exact usage is reported by responses. This
  supports a proxy experiment, not exact mapping of either configured Qwen endpoint.
- [Alibaba OpenSearch tokenizer API](https://help.aliyun.com/zh/open-search/search-platform/developer-reference/token-calculation):
  exists for a different service and listed model IDs. Its qwen-max entry is not proof
  of Token Plan qwen3.8-max/flash compatibility. No cross-service adapter was invented.
- [OpenAI counting API](https://developers.openai.com/api/docs/guides/token-counting):
  counts Responses input including message structure. It is not automatically equivalent
  to this gateway's Chat Completions serialization or isolated SkillContext. No call
  was made, and no request-count subtraction is claimed to isolate exact Skill tokens.
- [Sub2API route registry](https://github.com/Wei-Shaw/sub2api/blob/main/docs/COMPOSITE_GROUPS.md):
  public_model, upstream_model and endpoint-specific routes can differ. A count_tokens
  route alone does not establish equivalence to the solver's chat_completions route.
- [Pinned tiktoken source](https://github.com/openai/tiktoken/blob/0.14.0/tiktoken_ext/openai_public.py):
  provides a reproducible public o200k_base BPE asset/hash and segmentation pattern.
  No inference that the three API aliases use this vocabulary is made.

Chat/system framing, message separators, server-added instructions, template revisions,
multimodal data and alias remapping can change API input usage. Even empirical equality
on a finite sample would not alone prove model identity or universal precision. Future
strict support needs verified endpoint/model/vocabulary mapping and serialization,
plus authorized diverse-text usage checks with raw request/response evidence. Existing
API usage totals cannot be treated as ground truth for the isolated SkillContext.

## Implementation and reproducibility

New tokenizers.py loads local BPE bytes and verifies SHA256:
446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d.
It requires tiktoken==0.14.0, added as the optional routing dependency. The o200k_base
pattern follows the fixed upstream version; its hash, vocabulary hash, library version,
model name, provider and endpoint hash are included in CounterInfo.identity. License
attribution is in tokenizer-NOTICE.txt. The large vocabulary is cached, not committed.

Counts use encode_ordinary on the full assembled SkillContext. Special-token-looking
prose is literal text. This is genuine BPE tokenization, not character division; it is
exact for the proxy vocabulary only. CounterInfo.accuracy is always estimated and its
statement explicitly says target-model error is unbounded/unverified. No synthetic
fixture is selected in production. No runtime download or model/remote counting exists.

The explicit estimated_counter block must bind the exact configured model, provider,
and api_base, plus method/encoding/library version. Missing/mismatched fields, unknown
model aliases, bad version, missing/corrupt vocabulary or unsupported mode fail. This
binding detects configuration drift, not an invisible change behind a stable endpoint.
Changing a model/provider/URL requires consciously updating the binding. Credentials
remain in existing environment variables; counting reads no credentials.

Strict does not accept estimated_counter. Estimated requires the full block and an
estimated counter; there is no automatic downgrade. Existing strict defaults and route
schema/ID remain byte-for-byte compatible for the fixed P2 example. Estimated logs add
estimated_budget with effective_limit, safety_margin, unit and strict_guarantee=false.
Existing initial_skill_tokens/core_tokens/trial_tokens fields are proxy counts in this
mode; they are NOT provider usage. Provider billing usage remains in existing model
usage logs and is not replaced by estimates.

Selection uses floor(configured_budget / (1 + safety_margin)), computed with Decimal.
Example: budget=4000, margin=.25 -> proxy cap=3200. Margin is explicitly required and
can range from 0 to 10; .25 is a heuristic example, not a calibrated error bound.
Full-text trials and final recount, whole-file skip/accept, core-overflow failure and
five-route immutable sharing remain unchanged. Actual API token count may exceed 4000.
The budget never includes the rest of the solver prompt, framing or future messages.

## Setup and launch configurations

Three complete configurations are supplied; original configurations are untouched:

- configs/agent/routing-estimated-qwen-max.yaml (derived from qwen.yaml)
- configs/agent/routing-estimated-qwen-flash.yaml (derived from click15-flash-max.yaml)
- configs/agent/routing-estimated-gpt6-luna.yaml (derived from gpt6-luna.yaml)

All non-routing settings and role choices match their source configurations. These
files enable schema 1 and estimated routing; they do not run any tasks by themselves.
After reviewing endpoint/key availability, use a NEW output directory, not a strict
checkpoint. From the feature worktree:

```powershell
& D:/RSI/.venv-py314/Scripts/python.exe -m pip install -e ".[routing]"
& D:/RSI/.venv-py314/Scripts/python.exe -m rsi.skills.tokenizer_setup
# Or import an offline copy; SHA256 must match:
# python -m rsi.skills.tokenizer_setup --from-file <o200k_base.tiktoken>

# Template only; replace the task-manifest path. This was NOT executed:
& D:/RSI/.venv-py314/Scripts/python.exe -m rsi.cli run `
  --tasks <task-manifest.json> `
  --config configs/agent/routing-estimated-gpt6-luna.yaml `
  --label p21-luna-estimated
```

Switch the config filename to the Qwen Max/Flash example for those models. A literal
placeholder is not executable; provide the actual chosen task manifest. Setup downloads
only the public asset from openaipublic.blob.core.windows.net, with no key. Repeated
setup verifies existing bytes; corrupt existing files are not overwritten. Interrupted
writes can leave an invalid cache file; move that file aside explicitly, then retry.
Model execution is a separate user action and can consume quota; none was started here.

## Tests and limits

Full regression: **306 tests, 305 passed, 0 failed/errors, 1 existing Windows symlink
skip**, 40.354 seconds. All 283 P2 tests retained unchanged; 23 new P2.1 tests. Command:

```powershell
$env:TEMP='D:/RSI-core/_work/p21-tests'
$env:TMP=$env:TEMP
$env:PYTHONUTF8='1'
$env:LITELLM_LOCAL_MODEL_COST_MAP='True'
& D:/RSI/.venv-py314/Scripts/python.exe -m unittest discover -s tests -q
```

The routing extra and verified local vocabulary must be installed before these new
offline tests. Tests do not download assets or skip missing dependencies. Full log:
D:/RSI-core/_work/p21-tests/full-suite-1.log. Earlier targeted test: 21/21 passed.

Coverage: all model selections, strict unsupported behavior, unknown aliases, config
binding/version mismatch, corrupted/missing assets, offline setup, real BPE roundtrip
and reference-constructor equivalence, mode isolation, exact margin boundaries,
unchanged P2 route ID, five-route real-local-counter sharing with mock models, resume
without recount, changed binding/margin rejection and launch configuration compatibility.
No old assertions were removed or weakened. Real Docker/Harbor and provider usage
parity remain unverified. P0/P1, selector, scoring, checkpoint implementation and
upstream sources were not changed.

Offline audit D:/RSI-core/_work/p21-tests/tokenizer-audit.json records the builtin core
as 355 proxy BPE tokens for each model binding. Identical counts mean the SAME PROXY
was used, not that all three models share a tokenizer. No API usage is asserted.

## Git and stopping point

Start: b124b531cbc6ffef53e901843331ec7516dce128, skill-routing-p2 (preserved).
Branch: feature/skill-maintenance-routing. Accepted P2.1 is identified by the new
skill-routing-p2-tokenizer tag. No main merge, remote push, historical experiment edit
or P3 implementation. Code rollback does not restore experiment state; P2 cannot read
a new estimated-mode configuration. Stop here until the user explicitly authorizes P3.
