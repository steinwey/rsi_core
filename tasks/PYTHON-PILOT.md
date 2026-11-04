# Python 小范围验证：10 题训练 + 10 题冻结测评

来源：本地 SWE-Chain-Evo 缓存。按独立 SWE 单题执行，不继承上一题代码；训练期间只传递采纳后的 Skill。

每个项目 5 题训练、5 题测评，项目内按原始顺序前训练后测评；两个项目交替执行。排除已知的 Click lookup_default 题。

这衡量同项目后续任务上的迁移，不代表跨项目泛化或完整链成绩。题目以原始 source_instance_id 去重；评分数据保持独立，不进入模型输入。

| 阶段 | 顺序 | 项目 | 任务 ID | 目标 |
|---|---:|---|---|---|
| train | 1 | pallets/click | evo-f881cb75-001 | - Fix callable ``flag_value`` being instantiated when used as a default |
| train | 2 | sqlfluff/sqlfluff | evo-79f25adb-000 | - Oracle: parse TABLESPACE/STORAGE and other table/index options |
| train | 3 | pallets/click | evo-f881cb75-002 | - Fix speculative empty string check |
| train | 4 | sqlfluff/sqlfluff | evo-79f25adb-001 | - Fix for empty iterator bug that can occur from Jinja loops |
| train | 5 | pallets/click | evo-f881cb75-003 | - Document and fix command sanitizing with `shlex.split` |
| train | 6 | sqlfluff/sqlfluff | evo-79f25adb-002 | - Fix crash on malformed inline config using = syntax |
| train | 7 | pallets/click | evo-f881cb75-004 | - Allow debugger interactions in tests |
| train | 8 | sqlfluff/sqlfluff | evo-79f25adb-003 | - Add dbt 1.11 function() builtin to Jinja apply_dbt_builtins |
| train | 9 | pallets/click | evo-f881cb75-005 | - Expose original file descriptor in `CliRunner` |
| train | 10 | sqlfluff/sqlfluff | evo-79f25adb-004 | - Apply warning-level fixes even when no errors present |
| eval | 1 | pallets/click | evo-f881cb75-006 | - Reconcile default value passing and default activation |
| eval | 2 | sqlfluff/sqlfluff | evo-79f25adb-005 | - Add option for non-zero exit on large_file_skip |
| eval | 3 | pallets/click | evo-f881cb75-007 | - Show default string in prompt |
| eval | 4 | sqlfluff/sqlfluff | evo-79f25adb-006 | - Preserve mocked dbt `var()` placeholders under Jinja subscripting |
| eval | 5 | pallets/click | evo-f881cb75-008 | - Reduce blast-radius of `UNSET` in `default_map` |
| eval | 6 | sqlfluff/sqlfluff | evo-79f25adb-007 | - Handle shorthand-casted aggregates correctly in ST06 column ordering |
| eval | 7 | pallets/click | evo-f881cb75-009 | - Split string values from `default_map` for multi-value parameters |
| eval | 8 | sqlfluff/sqlfluff | evo-79f25adb-008 | - Ensure `pool.join()` is called in ParallelRunner |
| eval | 9 | pallets/click | evo-f881cb75-010 | - Auto-detect `type=UNPROCESSED` when `flag_value` has non-basic types |
| eval | 10 | sqlfluff/sqlfluff | evo-79f25adb-009 | - Force color output when --color is requested |

## 运行

在 `D:\RSI-core` 执行。离线检查不会调用模型或启动 Docker：

```powershell
& D:\RSI\.venv-py314\Scripts\python.exe -X utf8 scripts/run_python_pilot.py check
```

训练（10 题，每题 5 路，开启反思、轨迹压缩和 Skill 更新）：

```powershell
& D:\RSI\.venv-py314\Scripts\python.exe -u -X utf8 scripts/run_python_pilot.py train
```

训练完成后，用日志中的实际运行目录执行冻结测评。入口会检查训练任务与完成状态，并固定最终 Skill 的版本目录：

```powershell
& D:\RSI\.venv-py314\Scripts\python.exe -u -X utf8 scripts/run_python_pilot.py evaluate --training-run <训练运行目录>
```

测评沿用该训练运行保存的模型及压缩配置，仅冻结 Skill；每道题仍执行五路求解、题内反思、选择与评分。10 道测评题之间不更新 Skill。

两阶段分别由现有 monitor_experiment.py 监测，产生独立运行目录；不启用尚未支持的自动 train/test 工作流。启动前检查本阶段所有本地镜像，不自动下载。

当前只完成静态数据验证，Docker 镜像可用性待启动时检查；本构建没有运行训练或测评。

配置快照：`configs/agent/python-pilot.yaml`，轨迹摘要 12000 字符，Skill 8000 字符，不做语义审计。
训练或测评中断后，可运行 `python scripts/monitor_experiment.py --resume <原运行目录>`；仅跳过 checkpoint 已提交的题目，未提交题整题重跑。仍在运行的进程会被拒绝。详细边界见主 README 的 checkpoint 说明。
分割依据、原始实例 ID、文件哈希见 `tasks/python-pilot-split.json`。
