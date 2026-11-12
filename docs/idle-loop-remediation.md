# 空转(Idle-Loop)治理完整方案

针对 RSI-core / RSI-core-skill-routing 轨迹中确认的空转问题。基于对以下证据的分析:
`20261110-224917__click15-failures-except-`(deepseek-v4.1-flash,007 重复 grep 75 步 / 004 截断循环 42 败 / 001 修复误杀)、
`flash0-5way-225523`(qwen3.8-flash,rollout-3 同命令 31 连发 / rollout-4 heredoc 内围栏误判致死)、
`20261103-104450__click15_max_thinking_initial`(qwen3.8-max,探索不收敛 1801s 超时)。

---

## 空转的五个根因(按预算损耗排序)

| # | 根因 | 证据 | 损耗 |
|---|------|------|------|
| R1 | 确定性解码下的重复不动点:相同观察→相同响应→锁死;无思考通道的模型(deepseek-flash no-thinking、qwen-flash)最易陷入 | 007 同一 grep 重复 75 步;qwen-flash 31 连发 | 单轨迹最高 75% 预算 |
| R2 | 规格缺失 + 先验错位:milestone 一句话,模型按上游 HEAD 记忆找不存在的文档/规格;空 grep 因 `\| cat` 吞退出码得不到否定信号 | 007 找 docs 规格;luna-007 THOUGHT 长篇猜语义 | 触发 R1 的主要诱因 |
| R3 | 格式校验器缺陷:(a) heredoc 内的 markdown 围栏被误判 multiple_action_blocks 且该类免修复直接拒;(b) `original_command()` 不剥 DSML 尾标记,正确修复被判"改变了命令内容" | qwen-flash rollout-4 三连杀;deepseek 001 四次修复全误杀 | 整条 route 作废 |
| R4 | 截断循环:响应超 max_tokens=4096 → output_truncated 无条件拒修 → 反馈无具体指导 → 模型再生成同样长的内容 | deepseek 004 连刷 ~30 次 | 42 次失败调用 |
| R5 | 无预算感知:系统提示只写上限,过程中无剩余步数/时间回显;thinking 模型探索不收敛 | qwen-max 17 步纯探索撞 1800s | 整条 route 0 产出 |

另一个结构性问题(不直接烧预算但阻断自愈):**trace2skill 的 PASS 准入门把空转教训挡在 skill 之外**——
失败经验必须附带通过正式评测的诊断补丁,而空转轨迹恰恰最难修出补丁,于是最严重的失败模式永远没有学习信号。

---

## 方案总览:四层防线

```
第1层 预防   系统提示 + 任务模板(降低进入空转的概率)
第2层 破环   运行时确定性检测与注入(进入后快速脱出)
第3层 止损   修复校验器 bug + 硬性断路(脱不出则廉价终止)
第4层 自愈   trace2skill 过程病理旁路(把空转变成 skill 经验)
```

全部改动集中在 `rsi/agent/action_protocol.py`、`rsi/agent/execution.py`、
`rsi/skills/trace2skill/{pipeline,validation,prompts}.py` 和任务模板,互相独立、可分批上线。

---

## 第 1 层:预防(提示侧,零代码风险)

**P1. 任务模板加历史快照声明**(evo-swe 任务的 instructions 模板):

> This repository is a HISTORICAL snapshot: the milestone feature is NOT yet
> implemented. Its documentation and tests do not exist yet — that is expected.
> If a search returns nothing, do not keep searching; determine the intended
> semantics by reading related code paths and writing probe scripts, then implement.

直接拆掉 R2 的"找规格"陷阱。成功轨迹(202219 的 007)正是靠"读上下文+写探针"策略通过的。

**P2. `solver_step_limit_note` 补三条环境规则**:
- 无 `rg`,用 `grep -RIn`;
- 禁止输出 `<tool_call>`/`<｜DSML｜...>` 等原生工具协议标记,只输出一个 markdown bash 围栏块;
- 单次响应保持在 ~3000 token 内;写长文件时"先一条命令写文件,下一条命令运行",不要合并。

**P3.(可选,数据侧)** milestone 生成时从原 commit diff 自动附加涉及符号,如
`Touched: termui.prompt, Option.prompt_for_value`。一行即可消除大部分语义歧义。

## 第 2 层:破环(GuardedAgent 运行时检测)

在 `GuardedAgent.execute_action`(已有 pending_action 记录点)维护一个轻量状态:
最近 W=8 条已执行命令的规范化文本 + 输出 sha256(RecordingEnvironment 已算好 sha)。

**B1. 重复命令检测**:当前命令与历史重复时,在返回给模型的观察尾部注入(不改命令结果本体):

```
[monitor] This exact command was already run at step N with IDENTICAL output
(sha match). Repeating it cannot produce new information. Change strategy:
read the full context of files already matched, write a probe script, or
start editing source code. Steps used: {budget_calls}/{step_limit}.
```

连续第 2 次重复 → 注入升级为强制指令("You MUST NOT issue this command again");
连续第 K 次(默认 4)→ 不再执行命令(跳过沙盒,省时间),直接返回上述监视消息。

**B2. 空结果注释**:命令 returncode==0 且输出为空/仅分隔符时追加一行
`[monitor] Empty result. The content you expect may not exist in this snapshot; trust the repo over memory.`
补偿 `| cat` 吞掉的否定信号(R2)。

**B3. 预算回显**:每条观察尾部附 `[budget] calls {n}/{limit}, elapsed {t}/{T}s`;
过半且 `git diff` 仍为空时(RecordingEnvironment 可追踪是否出现过写文件/编辑类命令),追加一次
"budget half spent with no source modification — converge to a minimal change now"。治 R5。

配置项:`idle_guard: {enabled: true, window: 8, hard_stop_after: 4}`,默认开。

## 第 3 层:止损(修 action_protocol.py 的三个缺陷)

**F1. 围栏解析修正(治 qwen-flash 误杀)**:`_parse_action` 中
- 围栏识别只认行首 fences:`(?m)^```bash\n(.*?)\n^```$`;统计"其他代码块"前先把动作块挖掉,
  且忽略 heredoc 行内的 ```` 序列(不以行首三反引号+语言标签开头的不算开栏);
- 多个 bash 块若规范化后字节相同 → 去重为一个动作直接接受,不报 multiple_action_blocks。

**F2. 修复一致性校验修正(治 deepseek 001 误杀)**:`original_command()` 的尾部剥离正则追加
DSML 标记 `</?｜DSML｜[^<>]*>`(复用 `_DSML_TAG`);并在 `_parse_action` 中,
"唯一 bash 开栏 + 尾部只剩 DSML 关闭标记"的形态归类为 `mixed_tool_protocol`(可修复),
而非 `missing_or_unclosed_bash_block`。

**F3. 截断反馈可执行化(治 deepseek 004)**:`output_truncated` 的 feedback 由抽象的
"shorten or split" 改为具体模板:
"Your response exceeded the output limit and was discarded. Issue a SHORTER action now:
if you were writing a file, write it in 2+ parts (`>>` append), or make the minimal edit
with sed/python one-liner. Do not retry the same long response."
同时建议配置:求解模型 `max_tokens` 4096→8192(judge 已是 8192,成本影响可忽略)。

**F4. 纯 prose 响应免修复直调**:content 中无任何 ``` 时,直接走确定性拒绝分支
(省一次必然拒绝的修复 LLM 调用),反馈中原样附上格式模板。

## 第 4 层:自愈(trace2skill 过程病理旁路)

保持现有 PASS 准入门不动(它防的是 LLM 编造根因,动机正确)。新增一条**确定性旁路**,
原则:内容由 host 从轨迹事实机械提取,LLM 不参与断言,因此无需 PASS 验证。

**S1. 病理扫描器**(新模块 `rsi/skills/trace2skill/pathology.py`):输入 trajectory.json +
commands/index.jsonl + action-protocol.jsonl,输出机器可核验的病理事实:
- `repeated_command`: 同命令(规范化)执行 ≥3 次且输出 sha 相同 → 记录命令、首末步号、次数、耗步占比;
- `truncation_loop`: output_truncated ≥3 次;
- `format_death`: ActionFormatFailed 终止及其失败序列;
- `no_edit_exploration`: 全程无文件修改命令且 LimitsExceeded。

**S2. 模板化 memory item**:每类病理对应固定句式,槽位由扫描结果填充,`record_source:
'process_failure'`,绕过 `filter_failure_report` 的 PASS 门(在 pipeline.py 的 analyze()
里对失败 rollout 并行附加,不影响现有诊断 agent 流程)。例:

> On task {id}, {n}/{limit} steps were consumed repeating `{cmd}` with identical
> output. Rule: after 2 identical empty/unchanged search results, switch to reading
> matched files in full or writing a behavioral probe; never re-issue an identical command.

**S3. 成功-失败差分(可选增强)**:同题存在成功 rollout 时,把失败轨迹的病理摘要附进
成功分析(`analysis_prompt(True)`)的输入,引导 success model 总结"成功路径避开了什么"。
成功侧本就不需要 PASS 门,这是最高质量的经验来源。

**S4. `analysis_prompt(False)` 增补**:要求诊断 agent 报告中必须包含
"Resource usage pattern" 小节(即使修出 PASS,也要总结过程病理),避免根因类报告遗漏空转教训。

---

## 实施顺序与验证

| 批次 | 内容 | 验证 |
|------|------|------|
| 1(当天) | F1–F4(action_protocol.py)+ P2 | 新增单测:heredoc 含 ``` 的响应、重复双块、DSML 尾标记修复一致性、截断反馈文案;回放 001/004 与 qwen-flash rollout-4 的 response_content 作为固定用例 |
| 2(1–2 天) | B1–B3(execution.py/GuardedAgent)+ P1 | 构造 mock env 单测重复注入与硬停;smoke:用 click 8 题中 007 重跑,确认重复 ≤4 次即转向 |
| 3(2–3 天) | S1–S2(+S4) | pathology 扫描器对既有空转轨迹离线运行,人工审查生成的 memory items;合并进 skill 后跑一次 5-way 对照 |
| 4(可选) | P3 数据侧、S3 差分 | 对 click15 失败题集回测通过率对比 |

**预期收益**(按本次 8 题回测折算):007 类节省 ~70 步、004 类节省 ~35 次调用、
001 类整条 route 免于误杀;三条合计约占该次运行总预算的 30%+,且 ActionFormatFailed
误杀清零后,受影响题目从"必挂"变为正常概率通过。

## 风险与边界

- B1 的注入会改变观察内容,需在 RecordingEnvironment 的记录中保留原始输出与注入分离
  (monitor 行单独字段),避免污染 trace2skill 的证据链;
- F1 的行首围栏约束理论上可能拒绝某些缩进围栏的合法响应——回放历史轨迹全量验证后再上;
- S2 的模板 item 进入合并(merge_batch)后与 LLM 生成项同池,给 consolidation 的提示里
  注明 process_failure 项不可改写事实数字;
- 全部新行为挂配置开关,默认值可在 configs/ 里按 benchmark 分别启停。
