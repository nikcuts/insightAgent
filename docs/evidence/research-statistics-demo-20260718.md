# 科研代码真实模型演示记录

## 任务

隔离工作区 `/tmp/insightagent-research-demo-20260718` 中包含一个无第三方科学计算依赖的差异表达统计模块。任务要求修复 Benjamini-Hochberg 多重检验、可复现 bootstrap 置信区间、加权重复测量汇总和稳定 JSON 报告；固定验证命令为 `python -m pytest -q`。测试文件在整个 Agent 运行和后续修复中未修改。

## 基线

- `2 failed, 2 passed`
- 失败逻辑：全局随机状态导致 bootstrap 不可复现；重复测量汇总忽略权重且错误计算 SEM。

## 真实模型配置

- provider: `openai`（OpenAI-compatible adapter）
- model: `glm-5.2`
- source: 项目 `.env` 的 `BASE_URL`、`MODEL_ID` 和 `API_KEY`
- tool profile: `coding-basic`

## 运行结果

1. 复杂任务首轮：模型实际检查了源码，但 24 回合内重复读取，`iteration_limit`，122,033 tokens，未产生 patch。
2. 受限重试：模型服务请求超过时间预算，未产生 patch。
3. `research-statistics-demo-20260718-r5`：模型在 10.7 秒内通过 `edit_file` 实际修改 `src/biostats/report.py`，将 JSON 输出改为 `sort_keys=True`、紧凑 separators；trace 记录 `replacements=1`，run manifest 状态为 `completed`。
4. 其余统计核心在记录上述失败后由运行时维护者按任务规格补齐，未修改测试；最终 `4 passed`。

完整 trace：

- `research-statistics-demo-20260718.jsonl`
- `research-statistics-demo-20260718-r2.jsonl`
- `research-statistics-demo-20260718-r3.jsonl`
- `research-statistics-demo-20260718-r4.jsonl`
- `research-statistics-demo-20260718-r5.jsonl`

可展示代码位于本目录的 `src/biostats/`，原始验收测试位于 `tests/test_analysis.py`。

## 结论口径

这是一次“真实模型写入 + 运行时失败可观测 + 人工受控收敛”的科研代码演示，不应宣称为复杂任务的全自动成功。它适合在 PPT 中展示 Agent 的工具调用、trace 和失败分类，同时诚实呈现当前复杂任务收敛能力的边界。

