# 上游来源与复用边界

固定版本见 `upstreams.lock.json`。

| 来源 | 实际使用与位置 |
|---|---|
| Qwen-Applications/Trace2Skill | `vendor/Trace2Skill-upstream`，固定 `3d0b52a140f002a512930252b613c49048f7d5ac`；直接导入 ReActAgent、CombinedParallelSkillEvolver 的 MAP/REDUCE/APPLY、数据结构、解析和提示词模板 |
| google-research/reasoning-bank | 从 `RSI_DEPENDENCY_ROOT/vendor/reasoning-bank/third_party/src` 导入 mini-SWE-agent 的执行循环和 Docker 环境，并使用 SWE-bench 求解提示配置 |
| SWE-bench | 从现有环境调用固定版本 harness，执行真实测试并读取评分报告；没有重写测试判定 |
| Aiden0526/EvoArena | 读取固定版本的 SWE-Chain-Evo 任务和缓存镜像；不把任务参考修复作为 Skill 输入 |
| harbor-framework/harbor | 直接导入任务环境和 Verifier，支持 Terminal 任务的现有评分流程 |

Trace2Skill 为 Apache-2.0，许可证保留在上游源码中。ReasoningBank 与 Harbor 为 Apache-2.0；mini-SWE-agent 内置副本的许可见原依赖目录。其他项目按其固定源码的许可使用。

## SWE 适配层

`rsi/skills/trace2skill/` 提供模型传输与用量桥接、软件仓库分析提示词、五路评分适配、隔离诊断工具和文档编辑边界。Trace2Skill 的成功/失败分析、修改、合并、翻译及验证修复均使用 Max；外围求解与选择仍使用 Flash。

使用上游的 JSON 编辑流程，直接调用程序式 APPLY。没有运行上游 SpreadsheetBench CLI、电子表格 golden 文件评估器，也没有给分析器参考补丁。SWE 分析依据正式断言、归档轨迹和隔离的已提交代码。

编辑器直接运行官方完整 `run()`，不再覆写 MAP/REDUCE 异常处理，不再增加逐字引用检查或严格锚点拒绝器。分析报告由官方 Markdown 解析器提取。保留用户指定的每题五路更新与 Trace2Skill 全 Max、求解 Flash 分工。SWE 工具替代电子表格专用工具；不声称复现电子表格工作区及 PASS 标志验证。官方 quick_validate.py 已从 anthropics/skills 固定提交引入到其预期路径，原样保留；来源与哈希见 skills/skill-creator/upstream.json，许可证见 skills/skill-creator/LICENSE.txt。

仅应用一处日志路径兼容补丁：将外层的解析失败目录传入内部 SkillEvolver，防止运行时在仓库根目录创建 `parse_failures/`。补丁保存在 `vendor/Trace2Skill-upstream/rsi-patches/log-location.patch`，不改变分析或编辑算法。`rsi-source-manifest.json` 记录所用文件和补丁的哈希，加载时验证锁定提交与源码内容。源码稀疏检出保留分析、编辑器与运行时，未下载论文数据集。此接入不等于完整复现论文实验，也不预先承诺性能收益。
