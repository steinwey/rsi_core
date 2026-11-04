# 失败经验的正式 PASS 筛选

## 数据流

原流程：失败分析报告 → 官方 Markdown 解析 → 两类失败条目 → Skill Editor。

当前流程：失败报告与最终诊断补丁 → 正式 SWE-bench 评测记录 → 身份与哈希核对 → 仅 PASS 报告调用官方解析器 → Skill Editor。

五路求解、Selector、成功经验提取及官方编辑器的 MAP/REDUCE/APPLY 不变。

## 验证状态

| 状态 | 产生条件 | 经验处理 |
|---|---|---|
| PASS | 正式评测接口与对应实例报告均确认通过，提交补丁身份一致 | 允许解析失败条目 |
| FAIL | 正式接口与报告均确认未通过 | 保留报告，不提供经验 |
| UNVERIFIED | 无结果、记录缺失或不可信、任务/运行/补丁不一致 | 保留报告，不提供经验 |
| ERROR | 分析或评测异常、基础设施错误 | 保存错误，不提供经验 |

报告中的 PASS 字样、命令退出码、解析成功和旧 evaluate_passed.flag 都不产生 PASS。

## 诊断与评测

失败分析器可在其隔离仓库中通过原有诊断工具尝试修复，并调用 `evaluate_diagnostic_patch`。工具导出完整诊断补丁，调用现有 `SWEBackend.score()` 正式评测，不启动新求解器。

报告结束后，外围再次导出最终补丁。如果补丁没有可信评测记录或与最近提交的补丁哈希不同，自动正式评测最终补丁。若导出后又发生修改，最终哈希校验会拒绝旧 PASS。

评测不改变原路线的补丁或评分。未修改任何代码时，导出的仍是原失败补丁，不会凭原分析报告生成 PASS。

每路分析分配新的 UUID 作为 analysis_run_id。每次评测使用独立目录，并保存诊断补丁、提交预测、正式报告和验证记录；不复用旧评测目录。当前最终补丁单独保存。

验证记录包含实例 ID、分析运行 ID、补丁路径与 SHA256、最终补丁 SHA256、四态评测状态、SWE-bench 锁定提交、评测时间、结果路径与哈希、预测路径、结果摘要。

## 经验入口

失败报告统一经过 `filter_failure_report()`，目录报告和扁平 Markdown 使用相同规则。入口先核对当前 manifest、验证记录、评测器身份、最终补丁、评测补丁、预测内容和结果文件，再调用官方 `parse_items()`。

Failure Cause Items 和 Failure Memory Items 由同一门控管理。缺少验证记录默认拒绝；无有效条目或解析异常时，保留原报告并复制调试材料到 `failed_to_parse/<analysis_run_id>/`。

编辑器调用前统一执行 `filter_for_editor()`。它不信任记录中预填的失败 items，而是重新从通过验证的报告生成条目。成功记录保持原有处理。

本项目没有其他失败报告解析调用入口；直接使用 vendor 中的通用解析函数不属于 RSI 的经验导入流程。

## 统计与排查

每题目录下的 `trace2skill/failure-filter-summary.json` 包含：

- PASS/FAIL/UNVERIFIED/ERROR 数量。
- 接受和拒绝的报告数。
- 解析出的失败原因和失败经验数量。
- 提供给编辑器的失败报告数。
- 每个实例与分析运行的处理结果、拒绝原因和验证路径。

运行控制台会输出对应的 Failure Analysis Summary 与 Experience Filtering。

`trace2skill/editor-records.json` 是实际提供给编辑器的记录；失败记录附有验证记录路径和 analysis_run_id。`analysis-records.json` 保存筛选前的分析记录，其中失败 items 尚未解析，不可作为可导入经验使用。

各路的 `analysis-manifest.json`、`validation.json`、`diagnostic-final.patch` 和 `evaluations/<attempt_id>/` 用于追溯评测。

## 中断与历史记录

保持现有题级 checkpoint：未提交题目的旧目录在恢复时归档，新分析分配新的运行 ID。未完成评测留下的状态为 UNVERIFIED；新运行不能使用旧 manifest 或旧 PASS。旧版本历史报告没有验证记录时默认拒绝，不修改或删除原始文件。

## 离线测试

在仓库根目录执行：

```powershell
& 'D:/RSI/.venv-py314/Scripts/python.exe' -X utf8 -m unittest discover -s tests -p test_failure_pass_gate.py -v
& 'D:/RSI/.venv-py314/Scripts/python.exe' -X utf8 -m unittest discover -s tests -q
```

测试使用 mock 正式评测结果与确定性模型，不调用 API。覆盖四态、缺失记录、伪造 PASS、旧运行、补丁变化、结果篡改、目录/扁平报告、解析失败、成功经验不受影响，以及真实编辑器入口的过滤集成。
