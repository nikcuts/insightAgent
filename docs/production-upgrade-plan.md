# InsightAgent 生产级升级与汇报证据计划

## 目标

将 InsightAgent 从功能完整的研究型原型提升为可验证、可恢复、可审计的 Agent Runtime，并产出约 10 页技术汇报材料。

复杂度不是目标。每一项升级都必须同时具备：运行时实现、自动化测试、可重复实验和可解释的演示案例。

## 当前基线（2026-07-17）

- 分支：`codex/real-swebench-capability-cleanup`
- 测试：`286 passed`，约 `16.55s`（`uv run pytest -q`，包含审批、MCP trust、manifest、sandbox fail-closed 和真实 SWE 回归）
- 历史确定性轨迹：12 条，已有 legacy/resilient 对照报告
- 当前 SWE-style 输入：4 条有效 case，baseline 均以退出码 `1` 失败；这仍是本地小样本，不能据此宣称泛化能力；baseline 证据见 `docs/evidence/swe-style-baseline-2026-07-17.json`
- 真实模型运行：4 个本地小样本均 resolved；另完成 2 个官方 SWE-bench-Lite Flask issue，均 baseline 失败、只修改 1 个源文件、未修改测试、原始验证命令通过；证据见 `docs/evidence/swebench-lite-flask-4045-2026-07-17.json` 与 `docs/evidence/swebench-lite-flask-4992-2026-07-17.json`
- 能力与可观测性基准：9/9 通过，安全拒绝率 `100%`，脱敏回归通过；可靠性故障场景 2/2 通过；证据见 `docs/evidence/capability-baseline-2026-07-17.json`
- dry-run 仅用于验证评测管线，不计入 Agent 能力成绩
- 本轮 runtime hardening 已补齐：`approval_mode=interrupt`、run manifest/sidecar、默认拒绝 workspace MCP config、Docker sandbox fail-closed prototype；这些是 production-demo 能力，不等于多 worker 服务上线。

- Flask 真实案例暴露了三个边界：长历史触发 provider `messages` 错误、验证通过后的重复检查循环，以及普通工具编辑错误被误当成验证失败；上下文锚点、最小上下文恢复、验证后收敛和失败类型区分策略已加入运行时并有回归测试。两条官方案例仍是小样本，不能外推为通用成功率。

所有最终结果必须在同一批任务、同一模型配置和同一验收命令下，对比改造前后的运行结果。不能用预先写好的报告替代真实运行。

## 生产级升级切片

### 1. Capability-aware Security

在现有 `ToolSpec`、`ToolRisk`、`PermissionEnforcer` 和 `CommandValidator` 之上建立显式能力策略：

- 工具、命令、目录和 MCP Server 都有可解释的能力声明；
- 默认拒绝高风险和越界操作；
- 每次允许、拒绝、降级和需要确认的决策都产生审计事件；
- 审计输出经过统一脱敏，不能包含 API key、cookie、token 或私有路径中的敏感内容。

验收指标：危险命令阻断率 100%；越界路径阻断率 100%；安全回归用例无敏感信息泄漏。

### 2. Resumable and Fault-aware Runtime

利用现有 LangGraph checkpoint、session service、retry 和 time budget 能力，补齐可恢复执行协议：

- 工具超时、MCP 断连、模型失败和进程中断均产生明确失败状态；
- 从最近 checkpoint 继续时不重复执行已确认的副作用；
- 重试只针对可重试失败，永久失败必须快速收敛；
- 每次恢复都能解释恢复点、跳过的动作和待执行动作。

验收指标：故障注入场景的恢复成功率、重复副作用次数、失败解释完整率和 p95 恢复时间。

### 3. Evidence-driven SWE Loop

将现有 SWE-style runner 扩展为证据闭环：

- baseline 必须失败，且失败原因可分类；
- Agent 修改后必须运行原始验证命令；
- 禁止修改测试来制造通过；
- 记录 patch、测试输出、工具轨迹、迭代次数和失败模式；
- 只把真实模型运行且验证通过的 case 计入成功率。

验收指标：有效 case 数、resolved 数、测试修改违规数、验证覆盖率、平均工具调用数和成本/延迟。

### 4. Observable MCP Execution

在现有 Langfuse 和 JSONL trace 基础上统一运行证据：

- phase、tool、permission、risk、failure、retry、checkpoint 和 token usage 使用稳定事件类型；
- MCP server 的启动、工具发现、调用、超时、取消和重建都可追踪；
- trace、报告和控制台输出使用同一套脱敏器；
- 生成可直接放入 PPT 的运行轨迹摘要和指标表。

验收指标：关键事件完整率、trace 脱敏回归通过率、工具延迟 p50/p95、失败分类准确率。

## 实验矩阵

### A. 确定性安全场景

- 只读模式下的写文件、安装依赖和未知命令；
- `rm -rf`、路径穿越、符号链接和特殊文件；
- MCP Server 未配置、工具未知和调用超时；
- trace 中注入 API key、Bearer token、cookie 和私有路径。

### B. 确定性可靠性场景

- 测试失败后自修复；
- 网络错误和永久错误的重试抑制；
- 时间预算耗尽；
- checkpoint 中断后恢复；
- 多工具批次中前一个工具失败后的安全终止。

### C. SWE-style 任务

先扩充本地 fixture，再准备少量可复现的真实 SWE-bench-Lite case。每条 case 必须有失败 baseline、固定验证命令和未修改测试的验收规则。

## 10 页汇报结构

1. 问题：功能完整不等于生产可用
2. InsightAgent 当前架构与基线数据
3. 安全、可靠性和评测证据的缺口
4. 生产级目标与验收指标
5. 升级后的总体架构
6. Capability-aware Security 与审计链
7. Checkpoint 恢复与故障处理
8. Evidence-driven SWE Loop 与评测矩阵
9. 改造前后真实结果与案例演示
10. 创新点、局限性和后续路线

## 证据规则

- 没有真实运行结果的内容只能写成“计划”或“待验证”；
- dry-run、无效 baseline、环境错误不计入 Agent 成功率；
- 所有对照实验固定任务、模型、温度、超时、工具 profile 和验证命令；
- PPT 中同时展示成功案例、合理失败案例和安全阻断案例；
- 每个结论都能回溯到测试、trace、results.jsonl 或报告文件。

## 已执行的可复现实验

```bash
uv run python -m insightagent.evals.capability \
  --output docs/evidence/capability-baseline-2026-07-17.json
uv run pytest -q
```

该基准不调用外部模型或网络，覆盖读取、写入、安装、破坏性命令、路径越界、未知工具、重复拒绝和 trace 脱敏。它证明的是运行时安全边界，不等同于 SWE 修复成功率；后者必须等真实模型 case 运行后再记录。
