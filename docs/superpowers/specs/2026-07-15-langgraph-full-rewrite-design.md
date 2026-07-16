# LangGraph/LangChain/Langfuse 全面重写设计

## 目标

围绕一套统一框架栈全面重写 InsightAgent：

- LangGraph 是唯一的智能体编排运行时。
- LangChain 是唯一的模型与工具抽象层。
- Langfuse 是图、模型和工具执行的主要可观测性集成。

本次重写刻意不保留旧有的手写智能体循环、供应商客户端、JSON 会话运行时或自定义追踪管线作为兼容层。现有代码只能在其仍属于新框架边界内的领域逻辑时复用。

## 非目标

- 不保留 `CodeAgent.run_turn` 作为真实执行路径。
- 不保留 `api/providers.py` 作为主要供应商抽象。
- 不维护两个权威性相同的追踪系统。
- 引入 LangGraph 检查点后，不再让会话 JSON 成为事实来源。
- 不进行旧、新运行时同时支持生产执行的渐进式兼容迁移。

## 外部框架方向

当前 LangGraph 文档将其定义为面向长时运行、有状态智能体的底层编排运行时，LangChain 组件通常用于模型与工具。当前 LangChain 工具文档将工具定义为带有模式、可传递给聊天模型的可调用函数。当前 Langfuse 文档建议使用 LangChain/LangGraph 回调集成，并要求短生命周期应用显式执行 `flush` 或 `shutdown`。

参考资料：

- https://docs.langchain.com/oss/python/langgraph/overview
- https://docs.langchain.com/oss/python/langgraph/quickstart
- https://docs.langchain.com/oss/python/langchain/tools
- https://langfuse.com/integrations/frameworks/langchain

## 目标架构

创建新的 `insightagent.graph` 包，将运行时中心迁入其中。

```text
insightagent/
  graph/
    __init__.py
    state.py          # LangGraph 状态模式与归约器
    workflow.py       # 图构建与路由
    nodes.py          # 准备、模型、工具、修复、汇总与失败节点
    models.py         # LangChain 聊天模型初始化
    tools.py          # LangChain StructuredTool 定义
    retry.py          # 图工具的有界重试分类与退避
    repository_snapshot.py # 脱敏的仓库结构快照
    checkpoints.py    # 检查点与存储配置
    observability.py  # Langfuse 回调与本地调试钩子
    sessions.py       # 基于检查点的线程列表与转录导出
    project_memory.py # 项目记忆加载与注入
    usage.py          # LangChain provider token 用量累积
    runner.py         # 面向 CLI 的图运行器
```

CLI 变为轻量启动器：

```text
加载配置与环境变量
加载工作区与 MCP 配置
构建 LangChain 模型
构建 LangChain 工具
构建带检查点和回调的 LangGraph 工作流
调用或流式执行图
刷新 Langfuse
输出最终结果
```

## 运行时依赖

`pyproject.toml` 必须将框架栈声明为运行时依赖，不能依赖开发者机器上碰巧已经安装的包：

- `langgraph`
- `langchain`
- `langchain-core`
- `langchain-openai`
- `langchain-anthropic`
- `langchain-mcp-adapters`
- `langgraph-checkpoint-sqlite`
- `langfuse`

开发依赖继续包含 `pytest` 和 `pytest-timeout`。实现计划必须在核对已安装 API 后固定最低版本，尤其是检查点实现和 Langfuse 回调导入。

## 图状态

`AgentState` 是带类型的 LangGraph 状态，包含：

- `messages`：LangChain 消息，通过归约器只追加。
- `workspace`：字符串形式的绝对工作区路径。
- `task`：原始用户任务。
- `phase`：`plan | inspect | implement | verify | repair | summarize | done | failed`。
- `iteration`：模型/工具循环计数。
- `max_iterations`：图的防护上限。
- `verification_command`：从任务中提取的可选预期验证命令。
- `last_tool_error`：最近一次失败工具结果。
- `changed_files`：由写入/编辑工具修改的文件。
- `workspace_revision`：本轮成功工作区变更的单调版本号。
- `verified_workspace_revision`：最近一次退出码为 0 的验证所覆盖的工作区版本；后续变更会使其失效。
- `inspected_files`：已读取或搜索的文件。
- `verification_attempts`：已执行的验证命令与退出状态。
- `repair_attempts`：修复循环次数。
- `repair_action_completed`：当前修复循环是否已产生真实工作区变更；仅用于安排下一次验证，不能解除未解决错误。
- `phase_history`：用于测试与调试的有序阶段转换记录。
- `tool_events`：紧凑的结构化工具记录，包含工具名、参数摘要、权限、风险、命令类型、可重试性、抑制情况与失败类型。
- `final_answer`：面向用户的最终响应。

删除旧的 `TaskState`、`SlidingWindowMemory` 和 `ContextManager` 作为主要运行时对象。其有价值的行为由图节点或归约器重新实现。

`deadline_monotonic` 不是图状态，不得进入检查点。运行器为每次 `ainvoke`/`astream` 新建截止时间，并仅经该次调用的 `RunnableConfig["configurable"]["insightagent_deadline_monotonic"]` 传入节点；恢复新用户轮次也重新计算截止时间。

## 安全与持久化边界

所有会持久化、展示或发送到外部可观测性系统的数据必须先经过唯一的 `sanitize_for_model_trace_and_persistence()`。该函数在用户消息、模型消息、`ToolMessage`、工具参数摘要、工具结果、异常文本、`tool_events`、Langfuse 显式跨度、Langfuse 回调输入、JSONL 和 Markdown 转录的写入点统一调用。

- 以 `***REDACTED***` 替换 API key、token、password、secret、Authorization/header 值、dotenv 值、命令行密钥和匹配的高熵凭据；保留字段名、路径和失败类型，保证可调试性。
- 默认拒绝读取 `.env`、私钥、凭据目录和其他配置的敏感路径；若任务明确需要处理这类文件，工具只返回经过脱敏的摘要，原文不得进入模型消息、检查点、JSONL、转录或 Langfuse。
- `AgentState` 只保存经脱敏的消息和事件；工具执行需要的原始短生命周期值只能驻留在当前调用栈，调用结束即丢弃。
- 测试使用 canary secret，断言 SQLite 检查点、转录、JSONL、模型输入、伪 Langfuse 回调/跨度和异常事件均不含其原文。

一个 `thread_id` 在创建时绑定规范化的绝对工作区。用相同线程 ID 启动新轮次时，运行器必须先验证当前工作区一致；不一致时失败并要求创建新会话，不能跨仓库混用历史或工具上下文。

## 工作流

主图必须显式定义：

```text
START
-> prepare_task
-> inject_repository_snapshot
-> call_model
   -> execute_tools -> call_model | repair | fail
   -> summarize -> END
   -> action_required -> fail -> END
repair -> call_model | fail
```

路由规则：

- 模型产生工具调用时，通过 LangChain 工具调用执行。
- 没有工具调用且当前阶段仍需行动时，路由至修复/提示节点，不能直接接受文本回答。
- 工作区变更后，只有验证命令符合任务契约、结果含显式 `exit_code: 0` 且覆盖当前 `workspace_revision` 时才可路由至汇总；任意后续变更都会使旧验证失效。
- 迭代或修复预算耗尽时，路由至失败。
- 每次进入模型或工具节点前，若本次 `RunnableConfig["configurable"]["insightagent_deadline_monotonic"]` 已过期，则以 `time_budget_exceeded` 事件路由至失败，并保留部分状态。
- 在模型和工具执行期间，根据本次调用截止时间计算剩余墙钟时间，将供应商、工具和 shell 超时限制为该剩余时间；将执行中的超时或取消转换为 `phase=failed` 与 `time_budget_exceeded`。
- 运行时采用 `AsyncSqliteSaver` 和 `ainvoke`/`astream`。超时只有在子资源已终止并被等待回收后才能视为完成：模型和 MCP 必须使用可取消异步调用；不可协作的同步 Python 领域工具在 `tool_worker` 独立进程组中运行，shell 则由 `ToolRuntime` 父进程直接创建并持有独立进程组。后者不能经由通用 worker 间接启动，否则 worker 在 IPC 返回前异常退出会失去 shell 子树的清理所有权。超时后终止整个对应进程组并有界等待。同步 CLI 通过 `asyncio.run()` 调用该运行时。不得用线程超时返回伪装一个仍在后台执行的写操作。

这将取代手写的迭代循环。

图使用自定义 `execute_tools` 节点，而非普通的预构建工具节点，因为图控制状态不能只从模型可见的工具文本中推导。该节点调用 LangChain 工具，将 `ToolMessage` 追加到 `messages`，并显式返回 `changed_files`、`workspace_revision`、`verified_workspace_revision`、`inspected_files`、`last_tool_error`、`verification_attempts`、`repair_action_completed`、`phase`、`phase_history` 和 `tool_events` 的状态增量。它在节点内执行验证状态转换，拒绝没有退出码的验证结果；任一工具错误保持修复待处理状态，不能由后续纯文本直接汇总为完成。修复阶段只有真实工作区变更才能推进到下一次模型验证，且只有成功验证才解除根因。同一 `AIMessage` 的工具调用按顺序构成批次，首个错误或非零验证后，后续调用以 `tool_batch_aborted` 的 `ToolMessage` 受控跳过，既维持工具调用协议完整性，也不让后续成功掩盖根因。

## 模型

删除自定义供应商客户端作为生产运行时代码，改用 LangChain 聊天模型初始化：

- OpenAI 使用 `langchain_openai.ChatOpenAI`。
- SiliconFlow 使用 `langchain_openai.ChatOpenAI`。
- Anthropic 使用 `langchain_anthropic.ChatAnthropic`。

运行时只使用统一环境变量：`API_KEY`、`BASE_URL` 和 `MODEL_ID`。`RuntimeConfig.provider` 只决定上述三种 LangChain 客户端中的哪一种被构造；`RuntimeConfig.model` 或 `RuntimeConfig.base_url` 为 `None` 时，模型工厂才分别读取 `MODEL_ID` 或 `BASE_URL`，显式配置优先。`API_KEY` 始终从同名统一环境变量读取。三个值缺失、为空或不是字符串时，均抛出只包含对应统一变量名的 `ModelConfigurationError`；不得读取或回退到供应商专有环境变量。超时、最大输出 token、重试次数、温度和 top-p 均在此工厂中直接传给客户端。图执行前通过 `model.bind_tools(list(tools))` 绑定工具。测试使用伪造的 LangChain 聊天模型验证参数和客户端选择，并用 socket 哨兵保留真实 SDK 的无网络构造测试。

## 工具

将工具暴露方式重写为 LangChain `StructuredTool` 对象。

旧工具实现代码可以作为源材料，但新约定如下：

- 每个工具都有带类型的模式。
- 每个工具返回结构化文本或小型 JSON 可序列化对象。
- 权限检查、命令校验、工作区路径解析和失败分类均作为工具执行包装器。
- 工具执行通过 LangChain 回调发出 Langfuse 可观测跨度。

旧的 `ToolRegistry` 不再作为面向智能体的接口。可以替换为不对模型可见的 `ToolRuntime` 或 `ToolExecutor` 辅助组件。

`StructuredTool` 返回值只表示模型可见的结果。工具副作用和路由元数据由自定义 `execute_tools` 图节点捕获，并存入 `tool_events`。这保留当前 `ToolExecutionResult` 约定中有价值的部分，同时不再把旧注册表作为面向模型的抽象。

## 任务契约与 SWE 防护规则

重写必须保留既有的 SWE 风格安全与评测防护规则，并将其作为图运行时行为实现，不能只依赖提示词。

创建 `insightagent.graph.contracts`，在工具执行前后提取和执行任务契约。自定义 `execute_tools` 节点只能经图层的异步 `ContractAwareToolInvoker` 调用任意 `BaseTool`，先校验、对可能产生副作用或执行代码的 `ToolSpec` 在 `ToolContext.workspace` 捕获短生命周期 manifest/文件快照、调用 `tool.ainvoke()`、再校验结果；该入口同时覆盖内置、命令和 MCP 工具，禁止按工具来源绕过。快照记录常规文件的字节、mode、目录与非普通路径清单；若调用前已存在符号链接或其他非普通路径，则拒绝副作用调用，避免无法无损恢复的工作区拓扑。违反执行后规则时先恢复修改/删除文件、mode、新增文件和空目录，再向模型返回可见的工具错误；工具写入后抛异常或被取消时也必须先回滚，取消继续以 `CancelledError` 传播。回滚失败返回 `workspace_rollback_failed` 并保留原失败类别与异常摘要；快照绝不写入图状态、检查点或外部追踪。

必须具备以下契约行为：

- 当任务指定精确验证命令时，识别该命令并拒绝其他验证命令。
- 对仓库修复任务，要求在修改工作区前先检查仓库。
- 当任务指出失败测试时，要求在打补丁前阅读这些从失败变为通过的测试文件。
- 对 SWE 风格修复任务，除非任务明确要求修改测试，否则拒绝编辑测试文件。
- 当任务要求修复既有仓库时，拒绝只新增独立演示文件的修复。
- 拒绝未经请求的可选标志、替代命令或替代入口点，防止掩盖默认失败路径。
- 识别会从非测试文件中删除大量现有 Python 符号的破坏性重写。
- 验证失败后，要求下一次修改前先进行检查。

对 SWE 风格任务，任意写型 `execute_command` 默认被拒绝，只有任务指定的精确验证命令可以执行；未声明副作用的 MCP 工具默认被拒绝。被显式允许产生工作区副作用的工具执行前后必须记录完整受保护工作区 manifest 与文件内容；若发现测试文件修改或删除、仅新增独立演示文件、破坏性符号删除、特殊路径或违反检查顺序，运行时必须还原修改/删除的文件、移除本次新增文件和空目录，并返回 `task_contract` 错误。若还原本身失败，必须报告 `workspace_rollback_failed`，不能伪装成工作区已恢复的普通策略拒绝。

这些规则取代旧 `CodeAgent._enforce_task_contract` 行为，并必须由图测试覆盖。

## 记忆与检查点

以 LangGraph 检查点取代自定义会话 JSON。

第一阶段使用 `langgraph-checkpoint-sqlite` 的 `AsyncSqliteSaver` 作为本地持久化检查点实现。本次重写不包含自定义检查点实现。实现必须以 `thread_id` 配置异步图调用，支持通过 LangGraph 状态历史 API 使用 `checkpoint_id` 检查，并针对 SQLite 检查点实现断点续跑测试。

每个会话数据库为 `AsyncSqliteSaver` 与 InsightAgent 线程索引分别建立独立的异步 SQLite 连接；两者指向同一 `<session_dir>/checkpoints.sqlite3`，均启用 WAL 和 `busy_timeout`，不得在同一连接上混用 LangGraph 检查点事务和索引事务。任一连接创建失败或关闭失败时，必须继续尝试关闭所有已获得连接，再向调用方报告错误。会话索引使用该数据库中的 InsightAgent 专用表，不使用 JSON 索引文件。每个 `thread_id` 有跨进程锁，从读取最新状态、写入新轮输入到图结束/失败和索引提交始终串行；不同线程可并发。锁文件名必须是 `sha256(thread_id)` 的固定十六进制摘要，`thread_id` 限为 1 至 255 个不含路径分隔符的字符。索引更新处于事务中，且只在 `aget_state()` 返回非空、可读的检查点后提交；一旦 `BEGIN IMMEDIATE` 已排入 SQLite 队列，取消必须等待该操作结束并完成回滚后才可向上层传播；即使取消是在回滚等待期间到达，也不得吞掉 `CancelledError`。失败时不得留下已列出但不可读取的线程。

公开会话 ID 映射到 LangGraph `thread_id`：

```python
config = {"configurable": {"thread_id": session_id}}
```

`--checkpoint-id` 只能检查指定检查点：

```python
config = {"configurable": {"thread_id": session_id, "checkpoint_id": checkpoint_id}}
```

CLI 保留面向用户的会话操作，但其后端从旧 JSON 会话文件改为检查点历史：

- `--session-id`：使用或创建 LangGraph 线程 ID。
- `--session-dir`：SQLite 检查点数据库和会话元数据索引的位置。
- `--list-sessions`：从会话元数据索引列出已知线程 ID。
- `--export-transcript`：从检查点保存的 LangChain 消息与工具事件渲染 Markdown 转录。

在既有 `thread_id` 上启动新的用户轮次是显式运行器操作。`runner.start_turn` 追加新用户消息到检查点转录，保留长期对话历史和项目记忆上下文，并重置本轮字段：`task`、`phase`、`iteration`、`verification_command`、`last_tool_error`、`changed_files`、`inspected_files`、`verification_attempts`、`repair_attempts`、`phase_history`、`tool_events` 和 `final_answer`。上一轮的已完成或失败状态不得阻止下一轮执行。

第一阶段 `checkpoint_id` 只用于显式检查、转录导出和调试，不提供 replay 或 fork。真实工作区、shell 和 MCP 均可能有副作用，禁止从历史检查点重放。正常 `--session-id` 恢复从线程的最新检查点开始，然后执行新的 `start_turn`。未来如需 replay/fork，必须为每个分叉创建独立 copy-on-write 工作区，并禁用或要求严格幂等的 MCP 和命令副作用。

项目记忆变为 `prepare_task` 的输入步骤，通过添加系统/上下文消息实现，而不再使用独立记忆管理器。上下文裁剪变为 `trim_context` 图节点：`execute_tools` 先以 `max_tool_output_chars` 对模型可见工具结果做首次截断，再在下一次模型调用前以 `compact_tool_output_chars` 压缩已有 `ToolMessage` 与 `tool_events` 的结果正文。历史消息超过图服务的上限时，节点只使用 LangGraph `RemoveMessage` 删除最早的完整消息前缀，绝不让保留历史以孤立 `ToolMessage` 开头；节点位于初始快照、每次工具执行和修复循环之后、模型调用之前，并记录 `context_trim` 可观测性决策。

用量统计不再读取旧 `ModelResponse` 对象，而是从 LangChain 响应元数据和回调数据派生，再存入图状态和 Langfuse 元数据。

## MCP

MCP 仍是一等工具来源，但 MCP 工具在图构建前通过官方 `langchain-mcp-adapters` 转换为 LangChain 工具。图不应区分内置工具还是 MCP 工具；所有工具共享相同的 LangChain 接口、权限元数据、失败分类、截止时间配置和模型可见结果字段。不得自行假设任意 MCP JSON Schema 可以无损转换为临时 Pydantic 模型。

`MCPManager` 是异步运行时服务，不是旧同步客户端的兼容外观。它为每台启用服务器持有官方 `MultiServerMCPClient`、连接配置、该服务器的调用锁和活动调用任务集合；这些资源不能进入 `AgentState`。启动必须受每服务器 `startup_timeout` 限制。原生 MCP tools 只用 `load_mcp_tools(None, connection=..., ...)` 生成，再保留其模式和描述地包裹为带 `mcp_server` 元数据的 `BaseTool`；每次工具调用由官方适配器在同一 `ainvoke` 任务内建立和关闭 `ClientSession`，不能让管理器跨图任务持有 session。管理器还按最终工具名公开同代不可变 `ToolSpec` 映射，供图层统一调用策略使用。资源和 prompts 不由 `load_mcp_tools()` 生成：启动时分别用独立会话发现两类能力，一类不支持或失败不能隐藏另一类；每类发现与公开列表工具均遍历 MCP 分页游标。它们在自己的工具调用任务中通过 `client.session()` 和 MCP SDK 资源/提示词 API 转换为四个固定模式的 LangChain 工具，不将其误作任意 MCP tool schema。只有 `readOnlyHint=True` 的 MCP 工具可在 `read-only` 模式使用；未声明或带副作用/开放网络 hint 的工具必须以 `ToolPermission.MCP` 经权限策略拒绝。

每个 MCP 调用以 `RunnableConfig["configurable"]["insightagent_deadline_monotonic"]` 计算精确剩余时间，创建受管理任务并等待。超时或取消后必须请求取消并等待该任务终止；该任务的官方 session 上下文在同一任务中收尾，随后才从活动任务集合移除并将服务器标记为不可用。只有这一路径完成后才可返回 `time_budget_exceeded`。不能确认任务终止或任务内会话收尾时返回 `mcp_cancellation_unconfirmed`，绝不报告成功。`stop_all()`、刷新和重启使用相同取消并等待路径。测试必须覆盖可取消调用的 session 收尾、无遗留任务和无迟到工作区副作用，以及无法确认任务收尾的失败分支。

手写 `MCPClient`、JSON-RPC 协议和 stdio/HTTP 传输不再保留；MCP SDK 与 `langchain-mcp-adapters` 是唯一协议、传输和模式转换实现。MCP 生命周期事件记录为图调试事件和 Langfuse 观测。

## 可观测性

Langfuse 成为主要追踪汇聚端。

- 在 `observability.py` 创建 Langfuse 回调处理器。
- 通过 LangGraph 的 `invoke`/`stream` 配置传递回调。
- 添加追踪元数据：工作区、会话/线程 ID、供应商、模型、工具配置和任务哈希。
- 一次性 `insightagent-run` 在进程退出时关闭 Langfuse；交互式 `insightagent` 每轮只刷新，且仅在 REPL 进程退出时关闭 Langfuse 单例。
- 为非模型图节点、权限检查、任务契约拒绝、工具抑制、阶段变化、MCP 生命周期事件和上下文裁剪创建显式 Langfuse 观测/跨度，使 Langfuse 覆盖运行时决策，而不仅是 LLM 和工具调用。

本地控制台输出仍是展示层，而非权威追踪模型。JSONL 追踪只能作为从图事件生成的调试导出保留，不能作为主要遥测架构。

实现使用 `from langfuse.langchain import CallbackHandler` 与 `from langfuse import get_client`。

## 评测与调试追踪接口

继续支持 SWE 风格评测运行器，并让它直接调用 `insightagent.graph.runner`。如测试需要子进程隔离，`python -m insightagent.cli.run_task` 仍必须由图驱动，并暴露以下等价选项：

- `--trace-jsonl`
- `--tool-profile`
- `--allowed-tools`
- `--enable-mcp-server`
- `--max-wall-seconds`
- `--max-tool-iterations`
- `--max-output-tokens`
- `--language`
- `--no-trace`

`--trace-jsonl` 变为图事件的调试导出，包含阶段转换、工具事件、契约拒绝、验证尝试、最终状态以及可用时的 Langfuse 追踪 ID。评测报告可以继续链接该路径。

`--max-wall-seconds` 由图运行器、图路由和单次调用超时包装器共同执行。运行器计算单调时钟截止时间，经 `RunnableConfig["configurable"]["insightagent_deadline_monotonic"]` 传入本轮调用；每个长时循环边界在模型与工具执行前检查该时间。模型节点与工具节点在每次调用前计算剩余时间；供应商调用、Python 工具调用、MCP 调用和 shell 命令均限制为该剩余预算。超时收尾必须终止并等待所有进程组、MCP 会话和可取消任务，再将 `phase=failed`，记录 `time_budget_exceeded`，导出部分状态，并仍在 `finally` 中执行 Langfuse 刷新/关闭。

评测运行器必须将图的 `done`、`failed`、初始化异常、模型异常、时间预算耗尽和清理异常稳定映射为 `CaseRunResult`。无论图运行结果为何，评测都要分析已写入的部分补丁、执行外部验证、生成报告；不可用或不可读的 debug trace 必须在报告中标为 `unavailable`，而不是遗漏字段或中止整批评测。

工具选择语义必须保持确定：默认 profile 是 `coding-basic`；`analysis`、`all`、`mcp-playwright`、`mcp-github` 的内置工具与默认 MCP 服务器映射从现有 `tool_profiles.py` 迁入并由表驱动测试锁定。可重复或逗号分隔的 `--allowed-tools` 只能收窄当前 profile，若任一名称不在该 profile 内则模型启动前以配置错误失败；`--enable-mcp-server` 与 profile 的 MCP 选择取并集，`all` 表示全部已配置服务器，未知服务器同样在启动前失败。

## CLI 与公开命令

为方便用户继续保留脚本名：

- `insightagent`
- `insightagent-run`

但内部都调用图运行器。仅服务旧运行时的 CLI 选项将被删除或改名；除非能够干净地映射到新图运行时，否则不要求保留兼容标志。

`--no-trace` 只关闭控制台逐事件输出，不得关闭 `--trace-jsonl` 或 Langfuse。`--list-sessions` 不得创建模型、启动 MCP 或调用图，成功退出码为 `0`。配置、凭据、无效工具 profile、未知工具或未知 MCP 服务器错误退出码为 `2`；图以 `failed` 结束时 CLI 输出最终失败说明且退出码为 `1`。评测总是在写完报告后退出 `0`，除非显式 `--fail-on-unresolved`，此时存在未解决的可评测 case 则退出 `1`。

交互式斜杠命令改为基于图/会话服务实现：

- `/status`：最新图状态、阶段、线程 ID、检查点 ID 和工作区。
- `/cost`：从 LangChain/Langfuse 元数据派生的用量。
- `/memory`：由 `prepare_task` 注入的项目记忆。
- `/compact`：裁剪/汇总消息历史的图状态更新。
- `/clear`：新线程或状态重置操作。
- `/permissions`：当前权限模式与工具策略。
- `/export`：从检查点渲染的转录。
- `/mcp`：来自 MCP 管理器的 MCP 连接/工具状态。

## 移除计划

移除或退出下列模块的生产职责：

- `insightagent.agent.core`
- `insightagent.agent.session`
- `insightagent.agent.task_state`
- `insightagent.agent.memory`
- `insightagent.agent.context` 的运行时裁剪职责
- `insightagent.agent.task_contracts`
- `insightagent.api.providers`
- `insightagent.api.messages`
- `insightagent.tools.registry`
- `insightagent.telemetry.usage`
- `insightagent.telemetry.trace`
- 旧追踪事件生命周期作为主要追踪模型的职责

在新归属下保留或重写：

- 仓库快照逻辑，重写为图准备节点。
- 配置加载和 dotenv 行为。
- 运行时权限与命令校验语义。
- 文件、搜索、执行和代码分析工具行为，重写为 LangChain 工具。
- MCP 配置和管理器，并加入 LangChain 工具转换。
- SWE 风格评测运行器，更新为调用图运行器。
- 用量追踪，从 LangChain 响应元数据和 Langfuse 回调数据重写。
- 转录导出，从检查点保存的 LangChain 消息重写。
- 仓库快照与工具重试策略，分别收敛到 `graph.repository_snapshot` 与 `graph.retry`。

`GraphRunner` 是 MCP 管理器的唯一所有者。一次性 `insightagent-run` 以 `async with GraphRunner(...)` 执行一轮，在 finally 中逐 server best-effort `stop_all`，记录最终停止事件后关闭 JSONL 和 Langfuse。交互式 `insightagent` 在 REPL 外创建长期 `GraphRunner`，每轮只刷新可观测性，且只在 REPL 进程最终退出时停止 MCP、关闭 JSONL 和关闭 Langfuse。保留 `/mcp status|tools|restart|refresh`；命令只能调用运行器的 `restart_mcp()`/`refresh_mcp()`，二者标记工具代次，下一轮由运行器重新加载工具、重新 `bind_tools` 并编译/选择图，绝不让已编译图持有陈旧 MCP 工具实例。

## 测试策略

测试从旧类级行为迁移到图行为：

- 直接单元测试图节点。
- 使用伪造状态单元测试路由决策。
- 将带权限包装器的工具作为 LangChain 工具进行单元测试。
- 单元测试任务契约拒绝与恢复路径。
- 用固定线程 ID 单元测试检查点恢复。
- 单元测试无需凭据即可创建 Langfuse 回调。
- 单元测试 Langfuse 回调传递，以及成功和失败时的刷新/关闭。
- 在可行处保留使用伪模型/工具图的 CLI 冒烟测试。
- 更新 SWE 风格评测测试，断言图运行器行为与最终验证。
- 添加测试迁移矩阵，将每个旧运行时测试文件记录为：重写、删除但有替代覆盖，或保留用于非生产辅助行为。

旧 `FakeModelClient` 测试删除或重写为伪 LangChain 聊天模型响应测试。

## 验收标准

- `python -m pytest` 通过。
- 在干净环境中，`pip install -e .[dev]` 成功，且 `python -c "import langgraph, langchain_core, langfuse"` 成功。
- `insightagent-run --no-trace --workspace <tmp> --task <simple coding task>` 通过 LangGraph 而非 `CodeAgent` 执行。
- 静态防护扫描全部 `src/insightagent`，通过 AST 和静态 `importlib` 字符串检查找不到从旧限定模块 `insightagent.agent.core`、`insightagent.agent.session`、`insightagent.agent.task_contracts`、`insightagent.api.providers`、`insightagent.tools.registry`、`insightagent.telemetry.trace`、`insightagent.telemetry.usage` 导入的符号；被移除的旧模块文件本身必须不存在。新 `insightagent.graph.contracts.TaskContract` 是允许且必需的类型。
- LangChain 工具是 `BaseTool`/`StructuredTool` 实例，暴露带类型的模式，经由权限/路径/命令/任务契约包装器执行，传递回调配置，并返回模型可见文本或 JSON 可序列化对象。
- 内置工具和 MCP 工具在图构建前共享相同的 LangChain 工具接口。
- 伪模型图测试观察到 `plan -> implement -> verify -> repair -> verify -> summarize -> done`，并断言 `last_tool_error`、`changed_files`、`verification_attempts`、`iteration` 和 `phase_history`。
- 当仍需执行操作时，拒绝仅输出文本的模型响应。
- 任务契约测试覆盖精确验证命令执行、修改前检查、失败测试阅读、测试文件保护、必须修改既有非测试文件、拒绝独立演示文件、拒绝可选入口点、拒绝破坏性重写，以及失败后检查；另覆盖 shell 和 MCP 绕过尝试，断言受保护文件被还原。
- 会话可通过绑定工作区的 `thread_id` 恢复；特定检查点可通过 `checkpoint_id` 检查；从检查点保存的 LangChain 消息生成转录导出；跨进程并发的新轮次不丢失消息或会话索引。
- 在既有 `thread_id` 上启动新轮次会保留转录历史但重置本轮图字段，因此上一轮 `done` 或 `failed` 阶段不会阻塞下一项任务。
- 配置凭据时附加 Langfuse 回调；无凭据时干净地空操作；回调接收工作区/会话/供应商/模型/工具元数据，传入图 `invoke`/`stream` 配置，记录显式运行时观测，并在成功与失败时都于 `finally` 中刷新或关闭。
- SWE 风格评测可针对图运行器执行，并仍写入包含验证状态、补丁数据、失败模式和调试追踪路径的报告。
- 使用 `--max-wall-seconds` 时，执行中的慢速伪模型、Python 工具、MCP 工具和 shell 进程树均干净退出为 `phase=failed`，记录 `time_budget_exceeded`，保留部分状态，并在 `finally` 中刷新或关闭 Langfuse；测试断言无存活子进程、无未完成 future 且 MCP 客户端已关闭。
- canary secret 不会出现在 SQLite、转录、JSONL、模型输入、Langfuse 回调/跨度或异常事件中。
- SWE 图评测在成功、失败、异常和超时后均写出完整 `CaseRunResult`、补丁分析、验证状态和 debug trace 状态。
- 没有生产 CLI 路径导入旧 `CodeAgent` 循环或旧供应商客户端。

## 实施顺序

这是一项破坏性的内部重写。实现应拆分为小提交，但不得通过保留双运行时实现：

1. 添加依赖与图包骨架。
2. 实现状态、模型工厂、工具工厂和最小图。
3. 实现 SQLite 检查点、图会话服务与转录导出。
4. 将内置工具和 MCP 工具迁移为带状态产出的 LangChain 工具与 `execute_tools`。
5. 将任务契约和 SWE 防护规则迁入图策略。
6. 添加 Langfuse 回调与显式运行时观测。
7. 将 CLI、斜杠命令和评测运行器迁移至图运行器。
8. 删除旧运行时模块并重写测试。
9. 更新 README 和文档。
