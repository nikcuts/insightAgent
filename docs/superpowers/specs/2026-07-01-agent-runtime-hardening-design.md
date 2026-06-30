# Agent Runtime 落地改造设计

## 背景

真实 SiliconFlow API 探测显示：InsightAgent 的最小代码生成闭环可以完成写文件、执行命令和总结，但默认小模型会在工具调用上超时，开启 console trace 时会因为 `ConsoleTracer` 缺少 `_summarize_arguments` 直接中断任务。当前代码库还存在根目录包与 `src/` 包并行的问题，导致不同入口可能执行不同实现。

本设计使用 superpowers-zh 的 spec/plan 模式：先固定目标和边界，再把改造拆成可验证的小任务。

## 目标

把 InsightAgent 从 demo 级代码生成 loop 推进到可落地试用的本地 coding agent runtime。第一阶段聚焦 P0/P1：真实任务不能被 runtime 自身打断，配置和入口行为可预测，关键执行链路有回归测试和可展示证据。

## 非目标

- 不在本阶段重写整个 agent 架构。
- 不引入新的外部框架或数据库。
- 不把所有历史测试和未跟踪重构一次性清理完。
- 不提交 git commit；当前环境由用户决定是否提交。

## 成熟 Agent 系统参照

- Codex / Claude Code 类系统强调本地 workspace 工具、权限、上下文和可审计执行。
- Cursor / IDE Agent 类系统强调编辑器集成、精确文件修改和可回滚 diff。
- LangGraph 类系统强调持久状态、可恢复执行、人类介入和 trace。

InsightAgent 当前最接近第一类：本地工具执行型 coding agent。落地优先级应是稳定性、权限边界、验证闭环和可观测性，而不是先追求复杂多 agent 编排。

## 分阶段方案

### P0：真实任务不中断

修复已复现的 trace 崩溃；保证开启 trace 的真实代码生成任务能继续执行。为 `.env` 加载、provider client 构造、console trace 渲染和最小 agent loop 建立回归测试。

### P1：入口与配置一致

消除根目录 `insightagent/` 与 `src/insightagent/` 的行为分歧。短期内同步关键修复，长期只保留一个发布入口。配置层需要明确 provider、model、base_url、timeout、max_wall_seconds、tool_profile 的优先级。

### P1：执行护栏

把工具权限从粗粒度模式推进到可审计策略：每次工具调用记录权限、风险、cwd、命令分类、是否写 workspace、是否网络访问。破坏性命令默认拒绝，安装和网络命令需要显式策略。

### P2：任务闭环

固定 plan -> inspect -> edit -> verify -> summarize 状态机。最终回答必须列出修改文件、运行命令和结果。失败时必须说明阻塞原因和复现证据。

### P2：评测与组会展示

建立 trajectory benchmark：覆盖原生 tool_calls、文本协议恢复、代码块恢复、验证失败自修复、provider 超时、trace 渲染、MCP 不可用。输出成功率、平均迭代、失败分类和典型轨迹。

## 设计决策

推荐方案：先做 P0/P1 的“窄而硬”修复，再扩展能力。

替代方案一是直接做多 agent 编排，短期展示效果好，但会放大当前 runtime 不稳定问题。替代方案二是只换强模型，能提高成功率，但无法解决 trace、入口、权限和评测缺口。

## 成功标准

- 开启 trace 的最小真实代码生成任务不因 `ConsoleTracer` 崩溃。
- 针对 trace bug 有单元测试覆盖。
- `.env` 加载、provider 构造、trace 渲染、最小任务验证都有明确命令可复跑。
- 文档中有组会可用的能力边界和改造路线。

## 风险

- 网络和 provider 稳定性会影响真实 API 验证，需要区分 runtime bug 与远端超时。
- 当前 `src/` 目录是未跟踪重构内容，同步修改必要文件时要避免误认为它已经纳入版本控制。
- 本机没有 Node，不能使用 `npx superpowers-zh`，已采用 Codex installer 与 Windows junction 方式安装。

## 自检

- 无未决占位符。
- 范围聚焦到 agent runtime 落地，不包含无关 UI 或产品重写。
- 每个阶段都能由独立计划任务覆盖。
