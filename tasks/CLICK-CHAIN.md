# Click 长链：10 题训练，5 题测试

来源：本地缓存 SWE-Chain-Evo 的 `pallets__click_multistep_v25_L15_s0`，Python 仓库 `pallets/click`。

按原始零基索引 0–9 训练，10–14 测试，不打乱、不跳题。对应任务后缀 `000`–`009` 和 `010`–`014`。清单与原始数据哈希记录在 `click-chain-split.json`。

每题从该里程碑的固定快照开始，代码补丁不跨题传递。训练逐题继承 Skill，五路结果只提供监督；完成十题后固定最终 Skill，测试五题只评分预选答案。训练异常时不启动测试；普通评分 false 不终止流程。

这是同仓库顺序迁移实验；缓存中的部分题目在历史调试中已经使用，不应将结果描述为从未接触数据的盲测，也不能据此证明跨仓库泛化。

运行：

```powershell
& 'D:/RSI/.venv-py314/Scripts/python.exe' scripts/run_click_chain.py check
& 'D:/RSI/.venv-py314/Scripts/python.exe' -u -X utf8 scripts/run_click_chain.py run
```

两个阶段均由现有监测器监控。阶段目录、固定 Skill 路径和状态记录在 `_work/click-chain-*.workflow.json`。
中断后先用 `scripts/monitor_experiment.py --resume <训练目录>` 恢复训练，完成后执行 `scripts/run_click_chain.py evaluate --training-run <训练目录>`。测试阶段中断可直接恢复其测试目录。
