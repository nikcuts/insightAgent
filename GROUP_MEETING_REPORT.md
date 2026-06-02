# InsightAgent 组会汇报草稿

## 1. 项目背景

本阶段工作围绕一个自研代码代理系统 InsightAgent 展开。目标不是只调用一次大模型生成代码，而是实现一个可以在真实项目目录中执行任务、调用工具、保存上下文、验证结果并在失败时继续修复的 CodeAgent runtime。

最初版本的 InsightAgent 是一个基础 MVP：用户输入任务后，Agent 将消息发给大模型；如果模型返回工具调用，就执行工具并把结果返回给模型；如果没有工具调用，就输出最终回答。这个版本能完成简单任务，例如创建一个 Python 纸牌游戏并运行验证，但在更复杂任务中暴露出明显问题：

- 运行时间长：复杂前端任务需要生成多个文件、调用多轮工具，单轮模型响应容易超时。
- 上下文容易丢失：早期只使用滑动窗口记忆，历史工具调用和文件状态可能被截断或遗忘。
- 工具调用不稳定：部分模型会只在 Markdown 中描述工具 JSON，而不真正调用工具。
- 工具参数容易出错：生成较长文件时，模型可能输出不合法 JSON，导致工具参数解析失败。
- 安全边界不足：早期工具直接读写/执行，缺少 workspace 限制和权限梯度。
- 缺少可观测性：不知道一次任务用了多少轮、哪些工具被调用、失败发生在哪一步。
- 缺少会话恢复：任务中途超时或中断后，早期系统难以接续执行。

因此，后续版本借鉴了 `/home/dinghanchen/stuckin/claw-code-parity` 中体现出的 Claude Code 风格 runtime 思路，对 InsightAgent 做分阶段增强。

## 2. 参考对象：claw-code-parity 中体现的 Claude Code 风格机制

`/home/dinghanchen/stuckin/claw-code-parity` 不是单一脚本，而是一个围绕 Claude Code/Claw Code 行为进行 parity porting 的工程。它的核心启发不是某个单独函数，而是一整套 runtime 化设计：

- Agentic loop 不只是 while 循环，而是包含 session、usage、permission、hooks、tool executor、compaction、tracing 的运行时。
- 工具系统不是普通函数集合，而是有 schema、权限要求、结构化输出和行为测试的 tool surface。
- 上下文不是无限保留，也不是简单丢弃，而是通过 project memory、compaction、prompt construction、token usage 管理。
- CLI 不是只输出文本，而是提供 slash commands、session resume、cost/status/memory/export 等操作入口。
- 可靠性不只靠人工观察日志，而是通过 mock parity harness、结构化场景和自动化测试验证关键行为。
- 更高级的 agent 系统需要任务生命周期、事件状态机、恢复策略、插件/MCP/LSP 等扩展点。

在 `claw-code-parity/rust` 中可以看到明确的模块边界：

- `runtime`：conversation loop、session、config、permissions、compaction、hooks、MCP、task registry、usage。
- `tools`：bash/read/write/edit/grep/glob/Web/Todo/Agent/Skill/MCP/LSP 等工具入口。
- `commands`：slash command registry，例如 status、cost、compact、memory、permissions、export、session。
- `telemetry`：session trace 和结构化运行事件。
- `mock-anthropic-service` 与 parity harness：用确定性的 mock 服务测试多工具轮、权限拒绝、bash 输出、插件路径等。

这些机制说明：一个成熟 CodeAgent 的关键不只是“模型会写代码”，而是“运行时是否能稳定、可恢复、可观测、安全地驱动模型完成真实软件任务”。

## 3. InsightAgent 版本演进

### V1.0：基础 Agentic Loop

V1 实现了最小可用 CodeAgent：

- OpenAI-compatible / Anthropic 模型接入。
- `execute_command`、`read_file`、`write_file` 三个基础工具。
- 模型-工具循环：模型返回工具调用后执行工具，再把工具结果送回模型。
- 滑动窗口记忆：只保留最近若干条消息。

V1 可以完成简单任务，例如创建 `card_war.py` 并运行。但它的问题也非常明显：`write_file` 是全量覆盖，工具输出无限进入上下文，任务中断后没有 session，模型一旦不调用工具或工具参数格式错误，系统缺少修复机制。

### V2.0：上下文与记忆管理

V2 借鉴 Claude Code 的 project memory 和上下文预算思路，引入：

- `.codeagent.md` / `MEMORY.md` 项目记忆注入。
- 工具输出截断：大输出只保留头部和尾部。
- 子任务完成后的 micro-compaction：减少工具输出对后续上下文的污染。

这个版本解决的核心问题是 token 爆炸。真实代码任务中，`cat` 大文件、测试输出、错误日志会迅速占满上下文。如果不做截断和压缩，模型会越来越慢，也更容易遗忘真正重要的信息。

### V3.0：工具上下文与安全权限

V3 引入 `ToolContext`，将 workspace、权限模式、读写限制传入每个工具：

- 文件读写必须限制在 workspace 内，防止路径逃逸。
- `read-only` 与 `workspace-write` 权限分离。
- 破坏性 bash 命令默认拒绝。
- 新增 `edit_file`，支持局部字符串替换，降低全量覆盖风险。

这一步使工具系统从“能执行”变成“有边界地执行”。这也是 Claude Code 类系统最关键的工程能力之一：Agent 可以操作真实项目，但必须有权限梯度和工作区隔离。

### V4.0：检索与自愈

V4 增加：

- `grep_search`：基于正则/文本搜索代码库，而不是依赖向量检索。
- self-healing loop：工具失败后，自动把错误结果反馈给模型，并要求它进入修复模式。

这对真实代码任务很重要。模型第一次写代码经常会出现路径错误、语法错误、命令错误。成熟 Agent 不应该遇到错误就停止，而应该把错误作为下一轮输入继续修复。

### V5.0：Runtime 化

V5 是目前重点版本，项目位于 `/home/dinghanchen/stuckin/insightagent_v5`。它把前几版能力整合成一个更完整的 runtime：

- 配置加载：支持用户级、项目级、本地级 config，以及 CLI override。
- 会话持久化：每个任务有 session id，消息和工具结果保存到 `.insightagent/sessions`。
- 会话恢复与导出：支持 `--session-id` 接续任务，支持导出 Markdown transcript。
- 用量估计：每次模型调用记录输入/输出 token 估计值。
- Slash commands：支持 `/status`、`/cost`、`/memory`、`/compact`、`/clear`、`/permissions`、`/export`。
- 工具调用强制：`run_task` 默认要求模型实际调用工具，避免只输出 Markdown 示例。
- 工具参数修复：当模型返回 malformed JSON tool arguments 时，不直接崩溃，而是进入 repair loop。
- Trace 输出：运行时打印 user/model/tool/usage/final 等事件，便于复盘。

目前 V5 有 23 个单元测试通过，覆盖 agent loop、工具错误、自愈、配置、上下文截断、session、slash commands、权限、grep、usage 等。

## 4. 真实任务实验：从纸牌游戏到电商前端

### 简单任务：Python 纸牌游戏

纸牌游戏任务验证了基础链路：

1. 模型给出 plan。
2. 调用 `write_file` 写入 `card_war.py`。
3. 调用 `execute_command` 运行 `python3 card_war.py`。
4. 输出 10 回合模拟结果和最终分数。

这个任务说明 V1 级别 agent loop 已经可用。但该任务复杂度低，不能充分体现 Agent 的上下文管理、工具修复、长期任务能力。

### 复杂任务：电商平台前端 MVP

后续改用更复杂的任务：创建原生 HTML/CSS/JavaScript 电商前端 MVP，要求包含：

- 顶部导航
- 商品搜索
- 分类筛选
- 价格排序
- 商品网格
- 商品详情弹窗
- 购物车侧栏
- 数量调整
- 移除商品
- 总价计算
- 空购物车状态
- 响应式移动端布局
- 至少 12 个 mock 商品
- 文件结构验证与 JS 语法检查

实验中暴露出几个真实问题：

- 一次性让模型生成 4 个文件时，响应非常长，容易超时。
- 部分模型会把工具调用写成 Markdown JSON 示例，而不是实际 function call。
- 长 `write_file` 参数容易出现 JSON 解析错误。
- 没有强命令策略时，模型甚至可能尝试下载 Node 安装脚本，偏离任务边界。

V5 后续通过两类方式改进：

- runtime 改进：强制实际工具调用、捕获工具参数 JSON 错误、保存中间 session。
- 任务策略改进：把大任务拆成阶段，先生成 HTML/CSS，再续跑生成 JS/README 并验证。

最终在 `demo_ecommerce_qwen36` 中生成了完整前端项目：

- `index.html`
- `styles.css`
- `app.js`
- `README.md`

结构检查结果：

- 4 个文件均存在。
- `app.js` 包含 15 个商品。
- 搜索、筛选、排序、购物车、详情弹窗、数量、移除、总价等关键词全部覆盖。
- CSS 包含响应式布局。
- 安装用户目录 Node 后，`node --check app.js` 通过。

这个案例说明：当前 V5 已经具备一定真实项目能力，但复杂任务仍需要更强的任务分解、预算控制和验证策略。

## 5. InsightAgent V5 与 claw-code-parity 的对照

| 能力 | InsightAgent V5 当前状态 | claw-code-parity 中的参考形态 | 差距 |
|---|---|---|---|
| Agent loop | 支持模型-工具多轮循环、自愈 | `ConversationRuntime` 集成 session、hooks、permission、usage、tracing | V5 loop 仍较轻量，缺少 hook 和 streaming |
| Project memory | `.codeagent.md` / `MEMORY.md` 注入 | `CLAUDE.md`、`CLAUDE.local.md`、动态 system prompt builder | V5 记忆文件层级较少，缺少 git status/diff 注入 |
| Compaction | 工具输出截断、完成后压缩 | session-level compact summary，保留 recent tail，记录 compaction 元数据 | V5 summary 较粗糙，未使用模型生成高质量摘要 |
| Session | JSON session、resume、export | JSONL/JSON session、rotate、fork、latest、managed session control | V5 没有 fork/latest/rotation |
| Usage/cost | 字符估算 token | provider usage + cache token + pricing model | V5 估算精度低 |
| Tool system | execute/read/write/edit/grep | 40 个工具 spec，含 bash/file/grep/glob/web/todo/agent/skill/MCP/LSP | V5 工具面仍窄 |
| 权限 | read-only/workspace-write、workspace boundary、破坏命令拒绝 | read-only/workspace-write/danger-full-access/prompt + permission enforcer | V5 没有交互式 y/N prompt 和细粒度 allow/deny/ask |
| 检索 | regex grep_search | grep_search + glob_search + LSP symbols/diagnostics | V5 缺少 glob/LSP |
| CLI | 基础 REPL + slash commands | 更完整的 slash registry，支持 status/config/mcp/session/plugin/skills/diff | V5 命令数量少 |
| 可观测性 | Console trace + transcript | typed telemetry / lane events / session tracer | V5 事件仍偏日志，不是完整结构化状态机 |
| 可靠性测试 | 23 个 unit tests | mock Anthropic service + parity harness + 场景清单 | V5 缺少确定性端到端 mock harness |
| 任务编排 | 单 Agent 任务循环 | task registry、team/cron、worker boot、recovery recipes | V5 没有多任务/子代理/worker 控制面 |

## 6. 当前系统仍然 toy 的原因

虽然 V5 已经比 V1 完整很多，但距离完整 CodeAgent 系统仍有明显差距：

1. 工具面太窄：目前只有 bash/read/write/edit/grep，缺少 glob、todo、diff、git、LSP、web、MCP、sub-agent 等真实开发常用工具。
2. 缺少任务状态机：复杂任务没有明确的 `planning -> writing -> verifying -> repairing -> done/failed` 状态。
3. 缺少结构化验收：任务是否完成仍主要依赖模型总结，而不是 acceptance tests 作为一等对象。
4. 缺少稳定的长任务恢复：有 session，但还没有 latest、fork、checkpoint、retry policy。
5. 上下文压缩仍粗糙：当前 compaction 是规则摘要，不足以保留复杂任务的关键技术决策。
6. 权限系统不够细：没有 Claude Code 风格的 prompt approval，也没有 per-tool allow/deny/ask 配置。
7. 没有 streaming：长任务中用户只能等待完整响应，体感延迟高。
8. 没有 mock provider：测试仍偏单元测试，缺少“模型返回工具调用”的端到端确定性回放。
9. 没有 typed events：trace 可读，但机器难以基于事件自动恢复、统计和调度。
10. 模型能力依赖强：不同模型 function calling 稳定性差异很大，复杂任务需要分阶段 prompt 才稳定。

## 7. 后续改进方向

结合 `claw-code-parity` 的设计，下一阶段建议从“功能堆叠”转向“runtime 工程化”：

### 方向一：引入 Task Packet 与验收测试

把自然语言任务转为结构化任务包：

- objective：目标
- scope：允许修改范围
- files：预期文件
- acceptance_tests：必须执行的验证命令
- constraints：禁止安装、禁止联网、只能标准库等
- reporting_contract：最终汇报格式

这样可以减少 prompt 漂移，并让 Agent 每一步围绕验收标准推进。

### 方向二：增加阶段状态机

将任务执行显式拆成：

1. plan
2. inspect
3. implement
4. verify
5. repair
6. summarize

每个阶段都记录状态、输入、输出和失败原因。这样可以解决复杂任务“卡住但不知道卡在哪”的问题。

### 方向三：建设 mock parity harness

参考 `claw-code-parity` 的 mock Anthropic service，为 InsightAgent 建立确定性测试场景：

- streaming_text
- write_file_allowed
- write_file_denied
- malformed_tool_arguments
- no_tool_call_reprompt
- multi_tool_turn
- command_failure_self_healing
- ecommerce_frontend_task_skeleton

这样可以在不消耗真实 API 的情况下验证 Agent loop 的行为。

### 方向四：扩展工具系统

优先增加：

- `glob_search`：文件级搜索。
- `list_files`：快速目录快照。
- `git_diff` / `git_status`：让 Agent 感知改动范围。
- `todo_write`：记录多步骤任务进度。
- `node_check` / `python_check` 这类验证工具封装：减少模型乱写 shell。
- LSP 级工具：诊断、跳转定义、符号搜索。

### 方向五：增强权限与命令策略

引入更细权限：

- `read-only`
- `workspace-write`
- `prompt`
- `danger-full-access`

同时增加命令 allowlist/denylist，禁止模型在普通开发任务中自动执行安装脚本、curl/wget 下载脚本、系统包管理器等高风险行为。

### 方向六：提升上下文压缩质量

当前 V5 的 compaction 只统计角色和工具名。下一步应保留：

- 已创建/修改文件
- 当前任务目标
- 已通过/失败的验证命令
- 错误日志摘要
- 待完成事项
- 关键设计决策

复杂任务中，这比简单保留最近消息更重要。

### 方向七：结构化可观测性

把 trace 从纯文本升级为 typed events：

- `task.started`
- `model.requested`
- `tool.started`
- `tool.finished`
- `tool.failed`
- `repair.started`
- `verification.passed`
- `task.completed`

这样未来可以做 dashboard、自动 retry、统计平均耗时、失败类型分布等。

## 8. 汇报结论

本项目从一个基础 CodeAgent MVP 出发，逐步暴露出真实代码任务中的核心问题：长运行时间、上下文遗忘、工具调用不稳定、权限边界不足、失败恢复弱、可观测性差。通过参考 `claw-code-parity` 中体现的 Claude Code 风格机制，我们将 InsightAgent 演进到 V5：具备项目记忆、工具输出截断、局部编辑、workspace 安全、grep 检索、自愈、配置、会话、用量统计、slash commands 和 transcript 导出。

电商前端 MVP 实验说明，V5 已经能完成比纸牌游戏复杂得多的多文件项目任务；但实验也说明，当前系统还依赖人工拆分任务和模型稳定性。下一阶段的重点应是将“会写代码的 Agent”进一步升级为“可运行、可恢复、可验证、可审计的 Agent runtime”。

简而言之：

- V1 证明 Agent loop 可行。
- V2/V3/V4 解决上下文、安全、检索和失败修复。
- V5 开始具备 runtime 雏形。
- 下一阶段应补齐任务状态机、mock parity harness、结构化验收、权限策略、工具扩展和 typed telemetry。

这也是从 toy demo 走向真实 CodeAgent 系统的关键路径。

