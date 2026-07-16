# LangGraph 全面重写实现计划

> **供智能体工作者：** 必须逐项执行本计划，并使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans`。所有步骤均使用 `- [ ]` 复选框记录进度。

**目标：** 以 LangGraph、LangChain 和 Langfuse 完全替换 InsightAgent 的手写智能体循环、供应商客户端、JSON 会话运行时和主要追踪管线，同时保留既有工作区安全边界、SWE 防护规则、MCP 能力、CLI 命令和评测输出。

**架构：** 新增 `insightagent.graph` 作为唯一生产运行时。`runner.py` 构造模型、工具、检查点和图服务；`workflow.py` 编译显式状态图；节点仅返回可检查点化的状态增量，模型、MCP 客户端和文件句柄始终保留在运行时服务对象中，绝不写入图状态。LangGraph SQLite 检查点持久化线程状态；Langfuse v3 回调与显式跨度记录模型外运行时决策。

**技术栈：** Python 3.10+、LangGraph、LangChain Core、`langchain-openai`、`langchain-anthropic`、`langgraph-checkpoint-sqlite`、Langfuse v3、`pytest`、`pytest-timeout`。

---

## 文件结构与职责

| 路径 | 职责 |
| --- | --- |
| `pyproject.toml`、`uv.lock` | 声明并锁定运行时和开发依赖。 |
| `src/insightagent/graph/state.py` | `AgentState`、状态归约器、可检查点化事件类型和每轮重置字段。 |
| `src/insightagent/graph/models.py` | 从 `RuntimeConfig` 创建并绑定 LangChain 聊天模型。 |
| `src/insightagent/graph/tools.py` | 将现有领域工具转换为 `StructuredTool`，并提供共享的权限、失败分类、去重和截止时间计算。 |
| `src/insightagent/graph/contracts.py` | 提取并执行 SWE 风格任务契约。 |
| `src/insightagent/graph/nodes.py` | 准备、快照、模型、工具、验证、修复、汇总、失败和上下文裁剪节点。 |
| `src/insightagent/graph/workflow.py` | `StateGraph` 构建、条件路由与编译。 |
| `src/insightagent/graph/checkpoints.py` | SQLite 检查点数据库创建与关闭。 |
| `src/insightagent/graph/sessions.py` | 线程索引、检查点查询、转录导出和图状态读取。 |
| `src/insightagent/graph/observability.py` | Langfuse v3 客户端、回调、显式跨度、统一脱敏、JSONL 调试事件与关闭。 |
| `src/insightagent/graph/runner.py` | CLI/交互式/评测共用的图运行入口、`start_turn` 与资源生命周期。 |
| `src/insightagent/mcp/adapters.py`、`src/insightagent/mcp/manager.py` | 用官方 MCP 适配器生成 LangChain 工具；异步持有会话、调用任务、刷新、重启和状态查询。 |
| `src/insightagent/cli/run_task.py`、`main.py`、`slash_commands.py`、`smoke.py` | 只解析用户输入、调用图运行器和呈现结果。 |
| `src/insightagent/evals/swe_style.py` | 直接调用图运行器，继续生成评测报告、补丁和调试追踪路径。 |
| `tests/graph/` | 图状态、模型、工具、契约、工作流、检查点、可观测性和运行器测试。 |
| `tests/test_cli_graph.py`、`tests/test_no_legacy_runtime_imports.py` | CLI 冒烟和生产代码静态迁移防护。 |

旧的 `agent/core.py`、`agent/session.py`、`agent/task_state.py`、`agent/memory.py`、`agent/context.py`、`agent/task_contracts.py`、`api/messages.py`、`api/providers.py`、`api/resilience.py`、`tools/registry.py`、`telemetry/trace.py`、`telemetry/usage.py`，以及手写的 `mcp/client.py`、`mcp/protocol.py`、`mcp/transports.py` 和其旧运行时测试将在新图覆盖对应行为后删除。底层领域工具、运行时权限、命令分类、失败分类、配置和评测工作区分析继续保留，并由新图调用；MCP 协议与传输完全交由官方 `langchain-mcp-adapters` 及其依赖的 MCP SDK 实现。

### 任务 1：建立可复现的框架依赖基线

**文件：**

- 修改：`pyproject.toml`
- 修改：`uv.lock`
- 新建：`tests/graph/test_dependencies.py`

- [x] **步骤 1：写出依赖可导入的失败测试。**

```python
import importlib


def test_graph_runtime_dependencies_are_importable() -> None:
    for module in (
        "langgraph",
        "langchain_core",
        "langchain_openai",
        "langchain_anthropic",
        "langchain_mcp_adapters",
        "langfuse",
        "langgraph.checkpoint.sqlite",
    ):
        assert importlib.import_module(module)
```

- [x] **步骤 2：确认测试在当前环境失败。**

运行：`uv run pytest tests/graph/test_dependencies.py -q`

预期：因尚未在 `pyproject.toml` 声明框架依赖而失败。

- [x] **步骤 3：在项目元数据中声明完整运行时依赖。**

将 `pyproject.toml` 的空 `dependencies` 替换为：

```toml
dependencies = [
    "langchain>=1.0",
    "langchain-anthropic>=1.0",
    "langchain-core>=1.0",
    "langchain-mcp-adapters>=0.2",
    "langchain-openai>=1.0",
    "filelock>=3.0",
    "langfuse>=3,<4",
    "langgraph>=1.0",
    "langgraph-checkpoint-sqlite>=3.0",
]

[project.optional-dependencies]
dev = ["pytest>=7", "pytest-timeout>=2"]
```

执行 `uv lock` 更新锁文件；不得手工编辑 `uv.lock`。

- [x] **步骤 4：验证干净安装与导入。**

运行：`uv sync --extra dev && uv run pytest tests/graph/test_dependencies.py -q && uv run python -c "import langgraph, langchain_core, langfuse"`

预期：测试通过，导入命令退出码为 `0`。

- [ ] **步骤 5：提交依赖基线。**

```bash
git add pyproject.toml uv.lock tests/graph/test_dependencies.py
git commit -m "build: add langgraph runtime dependencies"
```

### 任务 2：定义仅含可序列化数据的图状态与测试伪件

**文件：**

- 新建：`src/insightagent/graph/__init__.py`
- 新建：`src/insightagent/graph/state.py`
- 新建：`tests/graph/__init__.py`
- 新建：`tests/graph/fakes.py`
- 新建：`tests/graph/test_state.py`

- [x] **步骤 1：写出状态归约和新轮次重置的失败测试。**

```python
from langchain_core.messages import AIMessage, HumanMessage

from insightagent.graph.state import new_turn_update


def test_new_turn_keeps_messages_and_resets_turn_scoped_fields() -> None:
    update = new_turn_update("修复 calc.py")

    assert update["messages"] == [HumanMessage(content="修复 calc.py")]
    assert update["phase"] == "plan"
    assert update["iteration"] == 0
    assert update["changed_files"] == []
    assert update["verification_attempts"] == []
    assert "deadline_monotonic" not in update


def test_message_reducer_appends_langchain_messages() -> None:
    from insightagent.graph.state import merge_messages

    merged = merge_messages([HumanMessage(content="a")], [AIMessage(content="b")])
    assert [message.content for message in merged] == ["a", "b"]
```

- [x] **步骤 2：确认状态测试失败。**

运行：`uv run pytest tests/graph/test_state.py -q`

预期：因 `insightagent.graph.state` 尚不存在而失败。

- [x] **步骤 3：实现 `AgentState` 和轮次状态工厂。**

`state.py` 使用 `TypedDict` 与 `Annotated` 定义状态；不把 `BaseChatModel`、`BaseTool`、`MCPManager`、回调处理器、SQLite 连接或 `Path` 对象放入状态。最小接口如下：

```python
from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from langchain_core.messages import AnyMessage, HumanMessage
from langgraph.graph.message import add_messages

Phase = Literal["plan", "inspect", "implement", "verify", "repair", "summarize", "done", "failed"]


def merge_messages(left: list[AnyMessage], right: list[AnyMessage]) -> list[AnyMessage]:
    return add_messages(left, right)


class AgentState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], merge_messages]
    workspace: str
    task: str
    phase: Phase
    iteration: int
    max_iterations: int
    verification_command: str | None
    last_tool_error: str | None
    changed_files: list[str]
    inspected_files: list[str]
    verification_attempts: list[dict[str, object]]
    repair_attempts: int
    phase_history: list[str]
    tool_events: list[dict[str, object]]
    usage: dict[str, int]
    final_answer: str | None


def new_turn_update(task: str) -> AgentState:
    return {
        "messages": [HumanMessage(content=task)],
        "task": task,
        "phase": "plan",
        "iteration": 0,
        "verification_command": None,
        "last_tool_error": None,
        "changed_files": [],
        "inspected_files": [],
        "verification_attempts": [],
        "repair_attempts": 0,
        "phase_history": ["plan"],
        "tool_events": [],
        "final_answer": None,
    }
```

`tests/graph/fakes.py` 提供可脚本化的 `Runnable`，它按顺序返回 `AIMessage`，并记录传入的 `RunnableConfig`；所有图测试共享该伪件，不能继续使用 `ModelClient`。

- [x] **步骤 4：运行状态测试。**

运行：`uv run pytest tests/graph/test_state.py -q`

预期：通过。

- [ ] **步骤 5：提交状态边界。**

```bash
git add src/insightagent/graph tests/graph
git commit -m "feat: define langgraph agent state"
```

### 任务 3：实现 LangChain 模型工厂并移除供应商客户端依赖

**文件：**

- 新建：`src/insightagent/graph/models.py`
- 新建：`tests/graph/test_models.py`
- 修改：`README.md`
- 新建：`.env.example`

- [x] **步骤 1：为统一环境变量、三种供应商和无密钥错误写失败测试。**

```python
import pytest

from insightagent.config import RuntimeConfig
from insightagent.graph.models import ModelConfigurationError, build_chat_model


def test_siliconflow_uses_unified_environment_configuration(
    monkeypatch: pytest.MonkeyPatch,
    model_factories: dict[str, list[RecordingChatModel]],
) -> None:
    monkeypatch.setenv("API_KEY", "test-key")
    monkeypatch.setenv("MODEL_ID", "Qwen/test")
    monkeypatch.setenv("BASE_URL", "https://api.example/v1")
    model = build_chat_model(RuntimeConfig(provider="siliconflow"), [])
    assert model.kwargs["model"] == "Qwen/test"
    assert model.kwargs["base_url"] == "https://api.example/v1"


def test_missing_unified_key_fails_before_graph_invocation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("API_KEY", raising=False)
    with pytest.raises(ModelConfigurationError, match="API_KEY"):
        build_chat_model(RuntimeConfig(provider="anthropic", model="claude-test"), [])
```

- [x] **步骤 2：确认模型工厂测试失败。**

运行：`uv run pytest tests/graph/test_models.py -q`

记录：旧实现仍读取供应商专有变量，结果为 `23 failed, 2 passed`。

- [x] **步骤 3：实现单一模型工厂。**

`build_chat_model(config, tools)` 必须：

- 对所有受支持供应商只读取 `API_KEY`；仅当 `config.model is None` 时读取 `MODEL_ID`，仅当 `config.base_url is None` 时读取 `BASE_URL`。显式配置优先，显式空值或非字符串必须报对应统一变量名，且不得回退。
- 不读取供应商专有环境变量；只设置旧变量而缺少统一变量时必须失败。
- 对 `openai` 与 `siliconflow` 构造 `ChatOpenAI(model=model, api_key=api_key, base_url=base_url, timeout=config.timeout, max_tokens=config.max_output_tokens, max_retries=config.max_retries, temperature=config.temperature, top_p=config.top_p)`。
- 对 `anthropic` 构造 `ChatAnthropic(model=model, api_key=api_key, base_url=base_url, timeout=config.timeout, max_tokens=config.max_output_tokens, max_retries=config.max_retries, temperature=config.temperature, top_p=config.top_p)`。锁定的 LangChain 版本将这些字段作为直接构造器参数；将 `top_p` 放入 `model_kwargs` 会产生废弃式警告。
- 在工厂末尾执行 `return model.bind_tools(list(tools))`。
- 真实 SDK 构造测试必须设置虚构的 `API_KEY` 并用 socket 哨兵禁止网络；不得用 mock 替换该测试中的 `ChatOpenAI` 或 `ChatAnthropic`。

`ModelConfigurationError` 仅用于配置/密钥错误；网络或模型调用错误由 LangChain 原始异常进入图失败节点。

- [x] **步骤 4：运行模型工厂、配置、状态、依赖与收集测试，并检查类型、格式和 diff。**

运行：

```bash
uv run pytest tests/graph/test_models.py tests/test_config.py tests/graph/test_state.py tests/graph/test_dependencies.py tests/test_pytest_collection.py -q
uv run --with pyright pyright --pythonversion 3.10 src/insightagent/graph/models.py tests/graph/test_models.py
uv run --with ruff ruff format --check src/insightagent/graph/models.py tests/graph/test_models.py
git diff --check
```

预期：全部通过；如格式检查失败，先运行 Ruff 格式化再重跑检查。

- [x] **步骤 5：同步统一变量文档与模板，不提交。**

将设计、README 和 `.env.example` 更新为 `API_KEY`、`BASE_URL` 和 `MODEL_ID` 模板；超时继续由项目配置或 `--timeout` 控制。模板只包含空的 Langfuse 占位符和统一变量，不读取或写入实际 `.env`。

### 任务 4：将领域工具转换为带策略包装的 LangChain 工具

**文件：**

- 新建：`src/insightagent/graph/tools.py`
- 修改：`src/insightagent/tools/execution_tools.py`
- 修改：`src/insightagent/cli/tool_profiles.py`
- 新建：`tests/graph/test_tools.py`
- 修改：`tests/test_tool_profiles.py`

- [x] **步骤 1：为工具模式、权限、去重、失败分类和剩余超时写失败测试。**

```python
from langchain_core.tools import BaseTool

from insightagent.graph.tools import ToolRuntime, build_builtin_tools
from insightagent.runtime.tool_context import ToolContext


def test_builtin_tools_are_langchain_tools(tmp_path) -> None:
    tools = build_builtin_tools(ToolContext(workspace=tmp_path))
    assert tools
    assert all(isinstance(tool, BaseTool) for tool in tools)
    assert next(tool for tool in tools if tool.name == "write_file").args_schema is not None


def test_read_only_policy_returns_model_visible_permission_error(tmp_path) -> None:
    runtime = ToolRuntime(ToolContext(workspace=tmp_path, permission_mode="read-only"))
    result = runtime.invoke("write_file", {"path": "a.py", "content": "x = 1\n"}, remaining_seconds=5.0)
    assert result.is_error is True
    assert "PermissionDenied" in result.content


def test_shell_timeout_is_capped_by_remaining_turn_budget(tmp_path) -> None:
    runtime = ToolRuntime(ToolContext(workspace=tmp_path))
    result = runtime.invoke("execute_command", {"command": "python -c 'import time; time.sleep(2)'", "timeout": 99}, remaining_seconds=0.1)
    assert result.failure_kind == "time_budget_exceeded"
```

- [x] **步骤 2：确认工具测试失败。**

运行：`uv run pytest tests/graph/test_tools.py -q`

预期：因新工具运行时不存在而失败。

- [x] **步骤 3：实现 `ToolRuntime` 与 `StructuredTool` 适配。**

`ToolRuntime` 在一个位置保留原 `ToolRegistry.execute()` 的领域行为：`ToolSpec` 推导、`PermissionEnforcer`、`CommandValidator`、`FailureClassifier`、只读调用去重、不可重试失败抑制和结构化 `ToolExecutionResult`。它不得导入或实例化 `ToolRegistry`。

实现接口：

`ToolRuntime.__init__(self, context: ToolContext, tools: list[Tool] | None = None)` 保存领域工具、规格、失败分类器、权限执行器、去重缓存和不可重试失败缓存；`specs(self) -> dict[str, ToolSpec]` 返回不可变规格副本；`invoke(self, name: str, arguments: dict[str, object], *, remaining_seconds: float | None) -> ToolExecutionResult` 执行完整策略链；`build_builtin_tools(context: ToolContext, runtime: ToolRuntime | None = None) -> list[BaseTool]` 返回由该运行时驱动的 `StructuredTool` 列表。

对内置工具，为每个稳定工具接口显式声明 Pydantic 参数模型并创建 `StructuredTool`，不从任意 JSON Schema 临时生成模型。工具函数调用 `runtime.invoke()` 并将 `ToolExecutionResult` 渲染为 JSON 可序列化字典；`execute_tools` 节点随后使用原始结果更新图状态。不得让模型直接访问 `ToolRuntime`；MCP 的任意 JSON Schema 只由任务 5 的官方适配器处理。

当工具参数存在 `timeout` 时，模型可见参数写入 `min(requested_timeout, max(1, floor(remaining_seconds)))`，但 shell 的实际等待上限同时受精确的剩余墙钟时间约束。文件、搜索、状态和 AST 等同步领域工具通过新的 `tool_worker` 进程组执行，父进程以 IPC 接收结构化 `ToolExecutionResult`；超时后先 `SIGTERM`、再 `SIGKILL` 整个进程组并有界等待，然后返回稳定的 `time_budget_exceeded`。`execute_command` 与 `run_verification` 不经通用 worker：`ToolRuntime` 父进程直接创建并持有 shell 的独立进程组，因此即使通用 worker 异常退出也不会失去 shell 子树的清理所有权；两者不再使用无法回收子树的 `subprocess.run(shell=True)`。MCP 超时将在任务 5 的异步适配器中实现。

- [x] **步骤 4：迁移工具 profile 的输入类型。**

将 `filter_tools()` 的类型改为 `Sequence[BaseTool]`，保持 `coding-basic`、`analysis` 和 MCP 选择逻辑不变。`--allowed-tools` 对不存在名称仍抛出 `ValueError`，不允许静默忽略。

- [x] **步骤 5：运行图工具与既有工具 profile 测试。**

运行：`uv run pytest tests/graph/test_tools.py tests/test_tool_profiles.py tests/test_tool_context.py tests/test_run_verification.py -q`

预期：通过。

- [ ] **步骤 6：提交工具适配。**

```bash
git add src/insightagent/graph/tools.py src/insightagent/tools/execution_tools.py src/insightagent/cli/tool_profiles.py tests/graph/test_tools.py tests/test_tool_profiles.py
git commit -m "feat: expose runtime tools through langchain"
```

### 任务 5：将 MCP 工具接入同一 LangChain 工具与异步生命周期边界

**文件：**

- 修改：`src/insightagent/graph/tools.py`
- 修改：`src/insightagent/mcp/adapters.py`
- 修改：`src/insightagent/mcp/manager.py`
- 修改：`src/insightagent/mcp/errors.py`
- 修改：`src/insightagent/runtime/permissions.py`
- 删除：`src/insightagent/mcp/client.py`
- 删除：`src/insightagent/mcp/protocol.py`
- 删除：`src/insightagent/mcp/transports.py`
- 新建：`tests/graph/test_mcp_tools.py`
- 修改：`tests/test_mcp_adapters.py`
- 修改：`tests/test_mcp_manager.py`
- 删除：`tests/test_mcp_client.py`
- 删除：`tests/test_mcp_protocol.py`
- 删除：`tests/test_mcp_stdio_transport.py`
- 删除：`tests/test_mcp_http_transport.py`

- [x] **步骤 1：写出官方 MCP 工具、资源/提示词与会话收尾的失败测试。**

```python
import asyncio
import contextlib
import sys
import time
from pathlib import Path

from langchain_core.tools import BaseTool
from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from insightagent.mcp.config import MCPConfig, MCPServerConfig
from insightagent.mcp.manager import MCPManager
from insightagent.runtime.tool_context import ToolContext


def test_manager_exposes_official_langchain_mcp_tools(tmp_path) -> None:
    fixture = Path("tests/fixtures/fake_mcp_stdio_server.py").resolve()
    config = MCPConfig(
        servers={
            "demo": MCPServerConfig.from_dict(
                "demo", {"command": sys.executable, "args": [str(fixture)]}
            )
        }
    )

    async def scenario() -> None:
        manager = MCPManager(config, ToolContext(workspace=tmp_path))
        assert await manager.start_enabled() == 1
        try:
            tools = await manager.get_tools()
            assert all(isinstance(tool, BaseTool) for tool in tools)
            assert {tool.name for tool in tools} >= {
                "mcp_demo_echo",
                "mcp_demo_list_resources",
                "mcp_demo_read_resource",
                "mcp_demo_list_prompts",
                "mcp_demo_get_prompt",
            }
            echo = next(tool for tool in tools if tool.name == "mcp_demo_echo")
            result = await echo.ainvoke({"text": "x"})
            assert result["mcp_server"] == "demo"
            assert result["content"][0]["text"] == "echo: x"
            resource = next(tool for tool in tools if tool.name == "mcp_demo_read_resource")
            resource_result = await resource.ainvoke({"uri": "fake://note"})
            assert resource_result["content"]["contents"][0]["text"] == "hello resource"
            prompt = next(tool for tool in tools if tool.name == "mcp_demo_get_prompt")
            prompt_result = await prompt.ainvoke({"name": "review", "arguments": {}})
            assert prompt_result["content"]["messages"][0]["content"]["text"] == "review this"
        finally:
            await manager.stop_all()

    asyncio.run(scenario())


class SlowArguments(BaseModel):
    text: str


class FakeMultiServerClient:
    def __init__(self) -> None:
        self.session_entries = 0
        self.session_closures = 0

    @contextlib.asynccontextmanager
    async def session(self, server_name: str):
        del server_name
        self.session_entries += 1
        try:
            yield object()
        finally:
            self.session_closures += 1


def test_mcp_timeout_cancels_and_closes_the_owned_session(tmp_path) -> None:
    client = FakeMultiServerClient()
    late_effect = tmp_path / "late-effect"

    async def slow_call(text: str) -> str:
        del text
        try:
            async with client.session("demo"):
                await asyncio.sleep(60)
        except asyncio.CancelledError:
            if client.session_entries != client.session_closures:
                late_effect.write_text("unsafe late mutation", encoding="utf-8")
            raise

    async def fake_tool_loader(session_arg, **kwargs):
        assert session_arg is None
        del kwargs
        return [
            StructuredTool(
                name="slow",
                description="slow test tool",
                args_schema=SlowArguments,
                coroutine=slow_call,
            )
        ]

    config = MCPConfig(
        servers={"demo": MCPServerConfig.from_dict("demo", {"command": "unused"})}
    )
    manager = MCPManager(
        config,
        ToolContext(workspace=tmp_path),
        client_factory=lambda connections: client,
        tool_loader=fake_tool_loader,
    )

    async def scenario() -> None:
        await manager.start_enabled()
        tool = next(tool for tool in await manager.get_tools() if tool.name == "mcp_demo_slow")
        started = time.monotonic()
        closures_before_call = client.session_closures
        try:
            result = await tool.ainvoke(
                {"text": "x"},
                config={"configurable": {"insightagent_deadline_monotonic": started + 0.01}},
            )
            assert result["failure_kind"] == "time_budget_exceeded"
            assert client.session_closures == closures_before_call + 1
            assert (await manager.status())["demo"]["active_tasks"] == 0
            await asyncio.sleep(0.05)
            assert late_effect.exists() is False
        finally:
            await manager.stop_all()

    asyncio.run(scenario())
```

同一测试文件再定义会在收到 `CancelledError` 后抛出 `RuntimeError("session cleanup failed")` 的伪 MCP 工具。对该工具调用必须返回 `failure_kind == "mcp_cancellation_unconfirmed"`、状态为失败，不能返回成功或 `time_budget_exceeded`；该分支验证“本地调用已停止”不足以证明任务内会话已经正确收尾。

- [x] **步骤 2：确认 MCP 测试失败。**

运行：`uv run pytest tests/graph/test_mcp_tools.py -q`

预期：因当前 MCP 适配器仍输出旧 `run()` 协议对象、管理器是同步 API 且没有会话所有权而失败。

- [x] **步骤 3：实现无同步兼容层的官方异步 MCP 运行时。**

`graph/tools.py` 新增唯一的 `remaining_seconds_from_config(config)`：优先读取 `insightagent_deadline_monotonic - time.monotonic()`，仅在没有截止时间时读取已有的 `remaining_seconds`；两者同时存在时取更小值。内置工具处理器和 MCP 包装器都调用它，任何非数值、布尔值或已耗尽预算均返回稳定的 `time_budget_exceeded` 结果。

`MCPManager(config, tool_context, *, client_factory=MultiServerMCPClient, tool_loader=load_mcp_tools)` 的生产公开接口全部为异步：`async start_enabled()`、`async get_tools()`、`async get_tool_specs()`、`async stop_all()`、`async restart_server()`、`async refresh_server()` 和 `async status()`；`get_tool_specs()` 返回按最终公开工具名索引、与 `get_tools()` 同一代的不可变 `ToolSpec` 副本。`tool_loader` 仅是测试缝，生产值固定为官方 `load_mcp_tools`。不提供 `asyncio.run()`、同步别名或旧 `MCPClient` 适配层。任务 10 的 `GraphRunner` 是该接口的第一个生产调用方，任务 11 的 `/mcp` 命令仅经 `GraphRunner` 调用它；任务 5 至任务 10 之间不运行旧 `CodeAgent` CLI，也不为其构造 `ToolRegistry` 兼容对象。

每个已启用服务器构造一个官方 `MultiServerMCPClient({server_name: connection})`，在 Python 3.10 兼容的当前任务超时包装内调用官方 `load_mcp_tools(None, connection=connection, ...)` 获取原生工具。官方适配器在每次工具 `ainvoke()` 内创建和关闭 session；因此 `MCPManager` 持有连接配置、每服务器 `asyncio.Lock` 和活动调用 `Task` 集合，但绝不跨图任务持有 `ClientSession` 或 `AsyncExitStack`。资源与 prompts 包装器也在其自己的调用任务中 `async with client.session(server_name)`。这样 AnyIO session 的进入、调用和退出始终发生在同一任务；这些运行时对象均绝不写入 `AgentState` 或检查点。`MCPServerConfig` 显式转换为官方连接字典：stdio 使用 `command`、`args`、展开后的 `env`，streamable HTTP 使用 `url`、展开后的 `headers`、`request_timeout`。启动失败或启动超时必须隔离到对应服务器并记录脱敏错误。

原生 MCP tool 必须只由官方 `load_mcp_tools()` 生成，禁止根据任意 `inputSchema` 自建 Pydantic 模型或字符串化多模态结果。适配器只包裹其已生成的 `BaseTool`，保留原 `args_schema`、描述与回调配置，并以配置的 `tool_prefix` 重命名。资源与 prompts 不属于 `load_mcp_tools()` 的输出：启动时必须在独立会话中分别发现资源与 prompts，一类不支持或失败不能隐藏另一类；发现和公开列表工具都遍历 `nextCursor` 分页。调用时在各自任务的 `client.session()` 上调用 MCP SDK 的 `list_resources()`、`read_resource(uri)`、`list_prompts()`、`get_prompt(name, arguments)`，建立四个固定模式的 `StructuredTool`（`list_resources`、`read_resource(uri)`、`list_prompts`、`get_prompt(name, arguments)`），其输出保留 MCP SDK 资源/消息内容的 JSON 安全摘要。它们不是任意 MCP JSON Schema 适配器，也不复用旧 `format_mcp_result()`；删除该函数和所有旧 `run()` 适配类。

`MCPToolRuntime` 复用 `PermissionEnforcer`、`FailureClassifier`、`ToolSpec`、`ToolExecutionResult` 与模型可见结果字段，而不调用仅拥有同步进程语义的 `ToolRuntime.invoke()`。原生 MCP annotations 驱动 `ToolSpec`：`readOnlyHint=True` 为只读低风险且 `mutates_workspace=False`；资源/提示词固定工具同样为只读；任何未声明或带副作用/开放网络 hint 的原生 MCP 工具保守标记为 `ToolPermission.MCP`、高风险、`uses_network=True`、`mutates_workspace=True`，并在 metadata 中记录 `mcp_server` 和副作用声明，供任务 6 的 SWE 契约拒绝未声明副作用的调用。`PermissionEnforcer.check()` 对 `ToolPermission.MCP` 在 `read-only` 模式直接拒绝，只有显式只读标记的 MCP 工具才可通过；`workspace-write` 模式仍须经过任务 6 的契约策略。结果始终包含 `mcp_server`、`failure_kind`、`retryable`、权限、风险以及保留原文本/内容块结构的 JSON 可序列化内容，不得降级为手写字符串格式化。

每次调用先创建并登记 `tool.ainvoke(arguments, config=...)` 任务，再以精确剩余预算等待。超时或外部取消时必须请求取消并 `await` 该任务终止；官方工具任务自身的 session 上下文会在同一任务退出时关闭。确认终止后从活动集合移除任务、将该服务器标记为不可用，最后才返回 `time_budget_exceeded`。若任务终止或其会话收尾不能确认完成，返回 `mcp_cancellation_unconfirmed`，保留该服务器失败状态且不得报告成功；测试伪件必须证明没有活动任务、调用会话已关闭且没有迟到的工作区副作用。`stop_all()`、重启和刷新采用同一取消并等待路径；刷新或重启完成后递增工具代次，使任务 11 的下一轮重新绑定工具。

`mcp/errors.py` 仅保留配置加载使用的 `MCPConfigError`；删除手写 `mcp/client.py`、`mcp/protocol.py`、`mcp/transports.py` 及其协议、客户端、stdio/HTTP 传输单测；保留并继续测试 MCP 配置解析、环境变量展开及脱敏。不得在生产代码中保留对被删模块、`MCPToolAdapter`、`MCPListResourcesTool`、`MCPReadResourceTool`、`MCPListPromptsTool`、`MCPGetPromptTool` 或 `format_mcp_result` 的导入。

- [x] **步骤 4：运行 MCP 回归测试。**

运行：`uv run pytest tests/graph/test_mcp_tools.py tests/test_mcp_adapters.py tests/test_mcp_manager.py tests/test_mcp_config.py -q`

预期：通过。

- [ ] **步骤 5：提交 MCP 迁移。**

```bash
git add src/insightagent/graph/tools.py src/insightagent/mcp src/insightagent/runtime/permissions.py tests/graph/test_mcp_tools.py tests/test_mcp_adapters.py tests/test_mcp_manager.py tests/test_mcp_config.py
git rm tests/test_mcp_client.py tests/test_mcp_protocol.py tests/test_mcp_stdio_transport.py tests/test_mcp_http_transport.py
git commit -m "refactor: replace custom mcp runtime with langchain adapters"
```

### 任务 6：迁移任务契约与仓库安全规则

**文件：**

- 新建：`src/insightagent/graph/contracts.py`
- 新建：`tests/graph/test_contracts.py`
- 修改：`src/insightagent/graph/tools.py`

- [x] **步骤 1：为所有契约拒绝路径写失败测试。**

```python
import pytest

from insightagent.graph.contracts import ContractViolation, extract_task_contract


@pytest.mark.parametrize(
    ("tool_name", "arguments", "expected"),
    [
        ("run_verification", {"command": "python -m pytest -q"}, "exact verification command"),
        ("edit_file", {"path": "src/calc.py", "old": "a", "new": "b"}, "inspect the repository"),
        ("edit_file", {"path": "tests/test_calc.py", "old": "a", "new": "b"}, "test-file edits"),
    ],
)
def test_repository_repair_contract_rejects_invalid_tool_actions(tool_name, arguments, expected) -> None:
    contract = extract_task_contract(
        "SWE-bench repository repair task. Run this exact verification command before finalizing: python -m pytest tests/test_calc.py -q"
    )
    with pytest.raises(ContractViolation, match=expected):
        contract.validate_before_tool(tool_name, arguments, inspected_files=[], changed_files=[], verification_failed=False)
```

再分别测试：读取指定失败测试后才可修改、已有非测试源文件必须改变、仅新增演示文件被拒绝、可选入口点被拒绝、破坏性 Python 符号删除被拒绝、验证失败后再次修改前必须检查；并分别用 `execute_command` 和会写入、删除、替换 `tests/test_target.py` 的异步 MCP `StructuredTool` 尝试修改测试文件、跳过检查修改源文件和删除 Python 符号，断言调用返回 `task_contract`、原测试文件字节与 mode 恢复、本次新增文件与空目录消失。额外覆盖工具写入后抛异常、取消、回滚失败、符号链接和已有特殊路径；MCP 伪工具必须先实际写入后返回成功，防止用调用前拒绝代替回滚测试。

- [x] **步骤 2：确认契约测试失败。**

运行：`uv run pytest tests/graph/test_contracts.py -q`

预期：因图契约模块不存在而失败。

- [x] **步骤 3：实现纯数据契约 API。**

`TaskContract` 不依赖节点、模型或工具对象，公开以下接口：

`ContractViolation(ValueError)` 表示可恢复的策略拒绝。`TaskContract.validate_before_tool(self, tool_name, arguments, *, inspected_files, changed_files, verification_failed, mutates_workspace=False, is_mcp=False)` 只在违反执行前顺序或命令要求时抛出该异常；`TaskContract.validate_after_tool(self, tool_name, arguments, result, *, changed_files, created_files=(), removed_symbols=None, unsafe_paths=())` 只在执行后发现破坏性改写、独立演示文件、测试文件变更/删除或特殊路径时抛出该异常。

从旧 `agent/task_contracts.py` 迁移精确命令标准化、测试文件判断和失败测试提取；从旧 `CodeAgent._enforce_task_contract` 迁移检查、修改、验证顺序。用 AST 对编辑前后 Python 文件的顶级函数、类和方法名进行集合比较：删除数量大于 `max(3, ceil(before_count * 0.4))` 时拒绝，并在结果中列出删除名称。对 SWE 任务，`execute_command` 只允许精确验证命令，未声明副作用的 MCP 默认拒绝；允许的副作用工具在调用前记录完整受保护工作区 manifest 与文件内容，违反后恢复修改/删除文件、移除本次新增文件和空目录。

`graph/tools.py` 新增异步 `ContractAwareToolInvoker(tool_context: ToolContext)` 与仅驻留调用栈的 `WorkspaceSnapshotService`。其唯一入口为 `await invoke(tool: BaseTool, spec: ToolSpec, arguments, config, contract, *, inspected_files, changed_files, verification_failed)`：先调用 `validate_before_tool()`；对 `spec.mutates_workspace`、`spec.executes_code` 或 `spec.required_permission is ToolPermission.MCP` 的工具，在调用前捕获工作区快照；若调用前清单含符号链接或其他非普通路径，拒绝副作用调用。再用原 `RunnableConfig` 执行 `await tool.ainvoke(arguments, config=config)`，最后以真实结果调用 `validate_after_tool()`。快照记录每个受保护常规文件的相对路径、字节、mode、目录与特殊路径 manifest，不进入模型、状态、检查点、转录或 Langfuse。违反执行后规则、工具异常或取消时，必须先恢复修改/删除文件及 mode、删除本次新增文件与空目录；取消继续传播，回滚失败以 `workspace_rollback_failed` 或其 `CancelledError` cause 保留可审计摘要。

`ContractViolation` 必须返回关联原工具名、`failure_kind="task_contract"` 的 JSON 可序列化模型结果，而不能抛出出图；快照、工具异常或回滚失败必须保留原失败类型与可审计摘要，其中未知工作区状态必须用 `failure_kind="workspace_rollback_failed"` 与普通契约拒绝区分。调用器不调用内置 `ToolRuntime.invoke()`，因此同样覆盖由 `MCPToolRuntime` 包裹的 MCP `BaseTool`。任务 7 的 `execute_tools` 节点只能通过这一入口调用任何工具，禁止以工具类型、名称或同步 `run()` 分流绕过契约。

- [x] **步骤 4：运行契约测试。**

运行：`uv run pytest tests/graph/test_contracts.py -q`

预期：通过。

- [ ] **步骤 5：提交任务契约迁移。**

```bash
git add src/insightagent/graph/contracts.py src/insightagent/graph/tools.py tests/graph/test_contracts.py
git commit -m "feat: enforce swe task contracts in graph tools"
```

### 任务 7：构建图节点、条件路由和端到端修复循环

**文件：**

- 新建：`src/insightagent/graph/nodes.py`
- 新建：`src/insightagent/graph/workflow.py`
- 新建：`tests/graph/test_workflow.py`
- 新建：`tests/graph/test_time_budget.py`

- [x] **步骤 1：写出状态转换与文本误完成的失败测试。**

```python
import asyncio

from insightagent.graph.workflow import build_graph
from tests.graph.fakes import ScriptedChatModel


def test_graph_repairs_failed_verification_then_completes(graph_services) -> None:
    graph = build_graph(graph_services.with_model(ScriptedChatModel.plan_edit_fail_repair_verify_finish()))
    result = asyncio.run(graph.ainvoke(graph_services.initial_state("修复 src/calc.py"), {"configurable": {"thread_id": "repair-1"}}))
    assert result["phase"] == "done"
    assert result["phase_history"] == ["plan", "inspect", "implement", "verify", "repair", "verify", "summarize", "done"]
    assert result["verification_attempts"][-1]["exit_code"] == 0
    assert result["changed_files"] == ["src/calc.py"]


def test_prose_only_response_is_nudged_while_action_is_required(graph_services) -> None:
    graph = build_graph(graph_services.with_model(ScriptedChatModel(["我已经修复完成"])))
    result = asyncio.run(graph.ainvoke(graph_services.initial_state("修复已有仓库"), {"configurable": {"thread_id": "nudge-1"}}))
    assert result["phase"] == "failed"
    assert "action_required" in result["tool_events"][-1]["failure_kind"]
```

- [x] **步骤 2：确认工作流测试失败。**

运行：`uv run pytest tests/graph/test_workflow.py tests/graph/test_time_budget.py -q`

预期：因图节点和图构建器尚不存在而失败。

- [x] **步骤 3：实现运行时服务与节点。**

`GraphServices` 是未进入状态的冻结数据类，持有 `model`、内置 `ToolRuntime`、带 `ToolContext` 的 `ContractAwareToolInvoker`、`dict[str, BaseTool]`、同名 `dict[str, ToolSpec]`、项目记忆、观察对象和配置上限。`workflow.build_graph(services, checkpointer)` 创建 `StateGraph(AgentState)`，注册下列节点：

```python
START -> prepare_task -> inject_repository_snapshot -> call_model
call_model -> execute_tools | summarize | action_required | fail
execute_tools -> call_model | repair | fail
repair -> call_model | fail
action_required -> fail
summarize -> END
fail -> END
```

节点规则：

- `prepare_task` 从用户消息提取任务契约、加入语言指令和项目记忆，设置 `phase="inspect"`。
- `inject_repository_snapshot` 调用现有 `build_repository_snapshot()`，追加系统上下文消息，不生成工具调用。
- `call_model` 只调用已 `bind_tools()` 的 LangChain 模型，增加 `iteration`，保存响应 `AIMessage`，并从 `response_metadata`/`usage_metadata` 累加用量。
- `execute_tools` 依次读取 `AIMessage.tool_calls`；每次都将同名 `ToolSpec` 传给 `ContractAwareToolInvoker.invoke()` 调用对应 `BaseTool`，返回关联 `tool_call_id` 的 `ToolMessage`，并更新 `changed_files`、`workspace_revision`、`verified_workspace_revision`、`inspected_files`、`verification_attempts`、`last_tool_error`、`repair_action_completed`、`tool_events` 和 `phase_history`。同一批次首个错误或非零验证后，后续调用必须以 `tool_batch_aborted` 响应受控跳过，不能执行或覆盖根因；修复阶段只有真实工作区变更才可推进到下一次验证，且只能由成功验证清除根因。不得以 MCP、内置工具或测试伪件为由绕过该入口。
- 验证是 `execute_tools` 内的受控状态转换：仅在已有工作区修改后接受 `run_verification` 或任务指定的精确命令；验证结果必须包含显式 `exit_code: <整数>`。只有退出码为 `0` 且验证对应当前 `workspace_revision` 时，模型的无工具响应才可进入 `summarize`；后续修改会使旧验证失效。失败、不完整或错误验证进入 `repair`。
- `repair` 追加简短的错误定位提示，并在 `repair_attempts` 达上限时转 `fail`。
- `summarize` 要求最后一条模型响应没有工具调用，写入 `final_answer` 并进入 `done`。
- `fail` 写入原因和 `final_answer`，进入 `failed`；失败阶段不得再调用模型。

图以 `AsyncSqliteSaver` 编译，运行器一律使用 `ainvoke`/`astream`，同步 CLI 由 `asyncio.run()` 包装。路由函数从本轮 `RunnableConfig` 读取截止时间，在调用模型或工具前检查迭代预算和剩余时间。模型调用使用可取消异步 LangChain API；同步领域工具和 shell 在独立进程组中执行，工具调用以任务 4 的剩余预算包装。任何 `TimeoutError`、`asyncio.TimeoutError`、`subprocess.TimeoutExpired` 或 MCP 截止时间错误都在终止并等待所有子资源后生成 `time_budget_exceeded` 事件并进入 `fail`。

- [x] **步骤 4：实现慢速模型、Python 工具、MCP 工具和 shell 命令测试。**

每个测试以 `RunnableConfig["configurable"]["insightagent_deadline_monotonic"] = time.monotonic()+0.05` 传入；断言图在 1 秒内返回，`phase == "failed"`、末条 `tool_events` 的 `failure_kind == "time_budget_exceeded"`，并保留已完成节点的状态。shell 测试启动会派生子进程的真实进程组，断言超时后父子 PID 均不存在；MCP 测试使用拒绝立即取消的伪会话，断言连接关闭和 future 已完成。另以 `GraphRunner.run_task(max_wall_seconds=...)` 覆盖慢模型、shell 进程树和 MCP 超时，禁止用未回收后台线程伪装超时成功。

- [x] **步骤 5：运行图循环测试。**

运行：`uv run pytest tests/graph/test_workflow.py tests/graph/test_time_budget.py -q`

预期：通过。

- [ ] **步骤 6：提交可执行图。**

```bash
git add src/insightagent/graph/nodes.py src/insightagent/graph/workflow.py tests/graph/test_workflow.py tests/graph/test_time_budget.py
git commit -m "feat: implement langgraph coding workflow"
```

### 任务 8：实现 SQLite 检查点、线程会话与转录导出

**文件：**

- 新建：`src/insightagent/graph/checkpoints.py`
- 新建：`src/insightagent/graph/sessions.py`
- 新建：`tests/graph/test_sessions.py`

- [x] **步骤 1：写出线程恢复、特定检查点和新轮次重置的失败测试。**

```python
def test_existing_thread_starts_a_new_turn_without_losing_history(graph_runner, tmp_path) -> None:
    first = graph_runner.run_task("创建 a.py", workspace=tmp_path, session_id="thread-1")
    second = graph_runner.run_task("检查 a.py", workspace=tmp_path, session_id="thread-1")
    assert second.thread_id == "thread-1"
    assert [message.content for message in second.state["messages"] if message.type == "human"] == ["创建 a.py", "检查 a.py"]
    assert second.state["iteration"] >= 1
    assert second.state["changed_files"] == []


def test_checkpoint_can_be_inspected_and_transcript_exported(graph_runner, tmp_path) -> None:
    outcome = graph_runner.run_task("创建 a.py", workspace=tmp_path, session_id="thread-2")
    snapshot = graph_runner.get_checkpoint("thread-2", outcome.checkpoint_id)
    transcript = graph_runner.export_transcript("thread-2", tmp_path / "transcript.md")
    assert snapshot.values["task"] == "创建 a.py"
    assert transcript.read_text(encoding="utf-8").startswith("# InsightAgent 会话转录")
```

- [x] **步骤 2：确认会话测试失败。**

运行：`uv run pytest tests/graph/test_sessions.py -q`

预期：因 SQLite 检查点与会话服务尚不存在而失败。

- [x] **步骤 3：实现 SQLite 检查点和会话索引。**

`open_checkpointer(session_dir)` 为同一个 `<session_dir>/checkpoints.sqlite3` 建立两条独立的异步 SQLite 连接：一条仅供 `AsyncSqliteSaver` 并首次调用 `setup()`，另一条仅供会话索引事务。两条连接均设置 WAL 与 `busy_timeout`，不得与 LangGraph 检查点事务共用连接；第二条创建失败时关闭第一条，任一关闭失败时仍尝试关闭另一条。在同一数据库创建 `insightagent_threads(thread_id primary key, workspace, created_at, updated_at)`；该索引只用于 `--list-sessions`，检查点仍是状态事实来源。`thread_id` 限为 1 至 255 个不含路径分隔符的字符，用 `filelock.FileLock(<session_dir>/locks/<sha256(thread_id)>.lock)` 从读取最新状态到图结束和索引提交串行化同一线程；索引更新与工作区绑定检查处于 SQLite 事务中。`BEGIN IMMEDIATE` 与后续索引 SQL 需在取消前完成已排队操作，取消路径必须等待回滚结束，避免遗留事务和写锁；若取消在回滚等待期间到达，回滚完成后仍要传播 `CancelledError`。测试传入 `../`、绝对路径和分隔符，断言锁始终在 `session_dir/locks` 内，并用跨平台 `spawn` 真实子进程在实际 `FileLock.acquire()` 返回“被锁阻塞”后覆盖同线程跨进程串行。

`GraphSessionService` 公开：

`GraphSessionService.list_threads(self) -> list[str]` 从线程索引返回稳定排序的线程 ID；`latest_state(self, thread_id) -> StateSnapshot` 调用 `await graph.aget_state({"configurable": {"thread_id": thread_id}})`；`checkpoint_state(self, thread_id, checkpoint_id) -> StateSnapshot` 传入两个 configurable 值；`history(self, thread_id) -> list[StateSnapshot]` 从 `graph.aget_state_history()` 收集；`export_markdown(self, thread_id, destination) -> Path` 写入完整消息和工具事件转录。第一阶段不暴露 checkpoint replay/fork。

`start_turn` 在持有线程锁时验证 `insightagent_threads.workspace == workspace.resolve()`；不一致即失败。验证通过后调用 `await graph.ainvoke(new_turn_update(task), {"configurable": {"thread_id": thread_id, "insightagent_deadline_monotonic": deadline_monotonic}})`；检查点会先合并新轮状态，再从 `START` 进入 `prepare_task`，因此旧消息历史被保留而上一轮的终态不会阻塞新轮。图调用返回或抛出后，服务都必须先用 `aget_state()` 读取非空检查点；只有读取成功才写入或更新时间索引，图未能留下可读检查点时绝不创建索引记录。显式 `checkpoint_id` 只用于 `aget_state()`、历史检查和转录；普通 `--session-id` 始终从最新检查点进入新轮次。转录按消息顺序输出用户、助手和工具消息，并在每轮尾部输出阶段、验证命令、退出码和文件改动。

- [x] **步骤 4：运行检查点测试。**

运行：`uv run pytest tests/graph/test_sessions.py -q`

预期：通过。

- [ ] **步骤 5：提交持久化会话。**

```bash
git add src/insightagent/graph/checkpoints.py src/insightagent/graph/sessions.py tests/graph/test_sessions.py
git commit -m "feat: persist graph sessions with sqlite checkpoints"
```

### 任务 9：接入 Langfuse v3 和图调试事件

**文件：**

- 新建：`src/insightagent/graph/observability.py`
- 新建：`tests/graph/test_observability.py`
- 修改：`src/insightagent/graph/nodes.py`

- [x] **步骤 1：写出有凭据、无凭据、元数据传播、异常清理与脱敏的失败测试。**

```python
from insightagent.graph.observability import build_observability


def test_observability_is_disabled_without_langfuse_credentials(monkeypatch) -> None:
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    observability = build_observability(workspace="/tmp/work", thread_id="t-1", provider="openai", model="gpt-test")
    assert observability.callbacks == []


def test_observability_flushes_after_graph_failure(fake_langfuse, monkeypatch) -> None:
    monkeypatch.setattr("insightagent.graph.observability.get_client", lambda: fake_langfuse)
    observability = build_observability(workspace="/tmp/work", thread_id="t-1", provider="openai", model="gpt-test")
    with observability.turn("run-task"):
        raise RuntimeError("boom")
    assert fake_langfuse.shutdown_called is True


def test_canary_secret_is_absent_from_every_observability_payload(fake_langfuse) -> None:
    observability = build_observability(workspace="/tmp/work", thread_id="t-1", provider="openai", model="gpt-test")
    observability.record_tool_event({"content": "api_key=canary-secret-123"})
    assert "canary-secret-123" not in fake_langfuse.serialized_payloads
```

- [x] **步骤 2：确认可观测性测试失败。**

运行：`uv run pytest tests/graph/test_observability.py -q`

预期：因图可观测性模块不存在而失败。

- [x] **步骤 3：实现 Langfuse v3 观察对象。**

在加载 dotenv 之后才导入或构造 Langfuse 客户端。凭据齐全时，以 `get_client()` 和 `CallbackHandler()` 创建回调；每次图调用配置必须含有：

```python
{
    "callbacks": callbacks,
    "run_name": "insightagent-turn",
    "metadata": {
        "langfuse_session_id": thread_id,
        "workspace": workspace,
        "provider": provider,
        "model": model,
        "tool_profile": tool_profile,
        "task_hash": task_hash,
    },
}
```

实现唯一 `sanitize_for_model_trace_and_persistence()`，并在消息进入模型/检查点、工具结果进入 `ToolMessage`/`tool_events`、异常进入图事件、数据进入 Langfuse 和转录前调用。`GraphObservability.turn()` 用 `get_client().start_as_current_observation(as_type="span", name="insightagent-turn")` 包住整轮；`node_span()` 覆盖准备、快照、权限、契约拒绝、工具抑制、阶段变化、MCP 生命周期和上下文裁剪。回调只接收已经脱敏的消息和工具结果，跨度输入不得含 API 密钥、完整环境变量或超过 `trace_max_chars` 的工具输出。`close()` 在 `finally` 中调用 `get_client().shutdown()`；未配置凭据时所有 API 均为空操作。测试从 SQLite、转录、JSONL、伪模型输入、伪回调、显式跨度和异常事件逐一验证 canary secret 不存在。

同时将旧 `JsonlTraceRecorder` 替换为只记录 `tool_events`、阶段转换、验证记录、最终状态与 `CallbackHandler.last_trace_id` 的 `GraphDebugRecorder`。控制台呈现只能读取该事件流，不得重新成为运行时真相来源。

- [x] **步骤 4：运行可观测性测试。**

运行：`uv run pytest tests/graph/test_observability.py -q`

预期：通过。

- [ ] **步骤 5：提交可观测性实现。**

```bash
git add src/insightagent/graph/observability.py src/insightagent/graph/nodes.py tests/graph/test_observability.py
git commit -m "feat: trace graph execution with langfuse"
```

### 任务 10：提供统一图运行器，并切换非交互式 CLI

**文件：**

- 新建：`src/insightagent/graph/runner.py`
- 修改：`src/insightagent/cli/run_task.py`
- 新建：`tests/graph/test_runner.py`
- 新建：`tests/test_cli_graph.py`
- 修改：`tests/test_cli_tool_profile_args.py`

- [x] **步骤 1：写出 CLI 和运行器的失败测试。**

```python
def test_run_task_cli_delegates_to_graph_runner(monkeypatch, tmp_path, capsys) -> None:
    called = {}

    def fake_run_task(**kwargs):
        called.update(kwargs)
        return type("Outcome", (), {"final_answer": "完成", "thread_id": "t-1", "checkpoint_id": "c-1"})()

    monkeypatch.setattr("insightagent.cli.run_task.run_task", fake_run_task)
    monkeypatch.setattr("sys.argv", ["insightagent-run", "--no-trace", "--workspace", str(tmp_path), "--task", "创建 a.py"])
    from insightagent.cli.run_task import main
    main()
    assert called["task"] == "创建 a.py"
    assert capsys.readouterr().out.strip() == "完成"


def test_production_cli_does_not_import_code_agent() -> None:
    import insightagent.cli.run_task as run_task
    assert "CodeAgent" not in run_task.__dict__
```

- [x] **步骤 2：确认运行器测试失败。**

运行：`uv run pytest tests/graph/test_runner.py tests/test_cli_graph.py -q`

预期：因 `runner.run_task()` 尚不存在且 CLI 仍导入 `CodeAgent` 而失败。

- [x] **步骤 3：实现 `GraphRunner` 和 `run_task()`。**

公开接口固定如下：

```python
@dataclass(frozen=True)
class RunOutcome:
    final_answer: str
    state: AgentState
    thread_id: str
    checkpoint_id: str | None
    trace_id: str | None


def run_task(
    *,
    task: str,
    workspace: Path,
    config: RuntimeConfig,
    session_id: str | None,
    checkpoint_id: str | None,
    tool_profile: str,
    allowed_tools: set[str] | None,
    enabled_mcp_servers: set[str],
    trace_jsonl: str | None,
    no_trace: bool,
) -> RunOutcome
```

一次性 `run_task()` 的资源顺序为：加载 dotenv、解析配置、创建工作区、以 `async with GraphRunner(...)` 打开检查点、加载项目记忆、选择内置与 MCP 工具、启动 MCP、建立模型和图、执行 `start_turn`、导出结果、在 `finally` 逐 server best-effort 停止 MCP、记录停止事件、关闭调试记录器、关闭 Langfuse 和关闭 SQLite。任何初始化失败也必须经过 `finally`。`--list-sessions` 在打开会话索引后立即返回，绝不创建模型或启动 MCP；`--no-trace` 只关闭控制台事件，不关闭 JSONL/Langfuse；配置/凭据/profile/工具/MCP server 错误退出 `2`，图终态 `failed` 退出 `1`。

CLI 保留 `--workspace`、`--task`、`--no-trace`、`--timeout`、`--max-tool-iterations`、`--max-output-tokens`、`--max-wall-seconds`、`--trace-jsonl`、`--tool-profile`、`--allowed-tools`、`--enable-mcp-server`、`--permission-mode`、`--language`、`--session-id`、`--checkpoint-id`、`--session-dir`、`--list-sessions` 和 `--export-transcript`。`coding-basic` 为默认 profile；`allowed-tools` 只能收窄 profile，`enable-mcp-server` 与 profile 默认 MCP 取并集，逗号/重复参数合并，未知名称在启动前失败。删除 `--allow-no-tool-final`，因为图路由通过状态而非兼容标志控制文本完成。

- [x] **步骤 4：运行 CLI 与运行器测试。**

运行：`uv run pytest tests/graph/test_runner.py tests/test_cli_graph.py tests/test_cli_tool_profile_args.py -q`

预期：通过。

- [ ] **步骤 5：提交 CLI 切换。**

```bash
git add src/insightagent/graph/runner.py src/insightagent/cli/run_task.py tests/graph/test_runner.py tests/test_cli_graph.py tests/test_cli_tool_profile_args.py
git commit -m "feat: run cli tasks through langgraph"
```

### 任务 11：迁移交互式 CLI、斜杠命令与用量呈现

**文件：**

- 修改：`src/insightagent/cli/main.py`
- 修改：`src/insightagent/cli/slash_commands.py`
- 修改：`src/insightagent/cli/smoke.py`
- 新建：`src/insightagent/graph/usage.py`
- 新建：`tests/graph/test_slash_commands.py`
- 修改：`tests/graph/test_runner.py`
- 删除：`tests/test_slash_commands.py`
- 修改：`tests/test_usage.py`

- [x] **步骤 1：写出斜杠命令基于图状态的失败测试。**

```python
def test_graph_slash_commands_read_checkpointed_state(graph_runner, tmp_path) -> None:
    graph_runner.run_task("创建 a.py", workspace=tmp_path, session_id="interactive-1")
    slash = GraphSlashCommandProcessor(graph_runner, thread_id="interactive-1")
    assert "thread_id=interactive-1" in slash.handle("/status")
    assert "total_tokens=" in slash.handle("/cost")
    assert "permission_mode=workspace-write" in slash.handle("/permissions")
```

- [x] **步骤 2：确认斜杠命令测试失败。**

运行：`uv run pytest tests/graph/test_slash_commands.py -q`

预期：因命令处理器仍接收 `CodeAgent` 而失败。

- [x] **步骤 3：重写命令处理器。**

交互式 `main.py` 在 REPL 外创建一个长期 `async with GraphRunner(...)`，每轮复用它；每轮结束只 `Langfuse.flush()`，最终退出才 `shutdown()`。`SlashCommandProcessor` 只接收 `GraphRunner`、`thread_id` 和 `workspace`，不持有裸 `MCPManager`。实现：

- `/status` 从最新 `StateSnapshot` 显示阶段、线程 ID、检查点 ID、工作区和迭代数。
- `/cost` 从 `state["usage"]` 和可用的 Langfuse 追踪 ID 显示输入、输出和总 token。
- `/memory` 显示本轮 `prepare_task` 注入的项目记忆文件名。
- `/compact` 调用 `graph.update_state()` 写入已裁剪的 `messages` 和工具事件摘要。
- `/clear` 创建新线程 ID，不修改旧线程。
- `/permissions` 显示本轮 `ToolContext.permission_mode` 与工具 profile。
- `/export` 调用 `GraphSessionService.export_markdown()`。
- `/mcp status|tools|restart|refresh` 调用 `GraphRunner` 的 MCP 方法；restart/refresh 增加工具代次，下一轮必须重新加载、绑定并编译图，当前已编译图不使用陈旧 MCP 实例。测试断言 refresh 后的下一轮不执行旧工具实例。

图内 `UsageAccumulator` 接收 LangChain `AIMessage.usage_metadata` 和 `response_metadata`，其中缺少 token 字段时记录 `0`，不得继续按字符估算。`main.py` 每轮调用 `GraphRunner.run_task()`，不保留持久 `CodeAgent`。

- [x] **步骤 4：运行交互式与用量测试。**

运行：`uv run pytest tests/graph/test_slash_commands.py tests/graph/test_runner.py tests/graph/test_usage.py tests/test_usage.py tests/test_smoke_cli.py -q`

预期：通过。

- [ ] **步骤 5：提交交互式迁移。**

```bash
git add src/insightagent/cli/main.py src/insightagent/cli/slash_commands.py src/insightagent/cli/smoke.py src/insightagent/graph/usage.py tests/graph/test_slash_commands.py tests/graph/test_runner.py tests/test_usage.py tests/test_smoke_cli.py
git commit -m "feat: migrate interactive cli to graph sessions"
```

### 任务 12：让 SWE 风格评测直接调用图运行器

**文件：**

- 修改：`src/insightagent/evals/swe_style.py`
- 修改：`tests/test_swe_style_eval.py`
- 新建：`tests/graph/test_swe_runner.py`

- [x] **步骤 1：写出评测不再拼接旧 CLI 子进程的失败测试。**

```python
from insightagent.evals.swe_style import run_case


def test_swe_runner_calls_graph_runner_and_keeps_debug_trace(monkeypatch, swe_case, tmp_path) -> None:
    calls = []

    def fake_run_task(**kwargs):
        calls.append(kwargs)
        return type("Outcome", (), {"final_answer": "完成", "state": {"phase": "done"}, "trace_id": "trace-1"})()

    monkeypatch.setattr("insightagent.evals.swe_style.graph_run_task", fake_run_task)
    result = run_case(swe_case, run_root=tmp_path, run_id="graph-eval", dry_run=False)
    assert calls[0]["task"].startswith("SWE-bench repository repair task")
    assert result.trace_path is not None
```

- [x] **步骤 2：确认评测测试失败。**

运行：`uv run pytest tests/graph/test_swe_runner.py tests/test_swe_style_eval.py -q`

预期：因评测仍调用 `build_agent_command()` 和旧子进程路径而失败。

- [x] **步骤 3：实现图运行器评测调用。**

保留 `load_cases()`、基线验证、工作区复制、变更分析、统一补丁、失败分类、报告和 prediction 导出。将 `run_case()` 中的智能体执行替换为 `graph_run_task()`，传入任务契约文本、临时工作区、`--max-wall-seconds` 映射的配置、`trace_jsonl` 路径、`coding-basic` profile 与评测固定权限。`CaseRunResult` 继续保存验证状态、补丁、失败模式、源/测试文件变化与调试追踪路径，并额外保存 Langfuse trace ID（可为空）。`run_case()` 必须捕获图初始化、模型、时间预算和清理异常，映射为稳定失败模式；随后仍分析部分工作区改动、执行外部验证并写报告。不存在或不可读的 trace 路径写为 `unavailable`。

子进程隔离仅保留给显式 `--subprocess` 选项；该选项必须执行 `python -m insightagent.cli.run_task`，因此仍由图驱动。删除 `build_agent_command()` 的旧 `CodeAgent` 假设后，更新相应测试。新增图返回 `failed`、图抛异常与部分写入后超时的测试，均断言报告、外部验证结果和 trace 状态存在；评测完成后默认退出 `0`，仅 `--fail-on-unresolved` 在存在未解决可评测 case 时退出 `1`。

- [x] **步骤 4：运行评测测试。**

运行：`uv run pytest tests/graph/test_swe_runner.py tests/test_swe_style_eval.py tests/test_swe_bench_lite_prepare.py -q`

预期：通过。

- [ ] **步骤 5：提交评测迁移。**

```bash
git add src/insightagent/evals/swe_style.py tests/graph/test_swe_runner.py tests/test_swe_style_eval.py
git commit -m "feat: run swe evaluations through graph runner"
```

### 任务 13：删除旧运行时并完成测试迁移矩阵

**文件：**

- 删除：`src/insightagent/agent/core.py`
- 删除：`src/insightagent/agent/session.py`
- 删除：`src/insightagent/agent/task_state.py`
- 删除：`src/insightagent/agent/memory.py`
- 删除：`src/insightagent/agent/context.py`
- 删除：`src/insightagent/agent/task_contracts.py`
- 删除：`src/insightagent/api/messages.py`
- 删除：`src/insightagent/api/providers.py`
- 删除：`src/insightagent/api/resilience.py`
- 删除：`src/insightagent/tools/registry.py`
- 删除：`src/insightagent/telemetry/trace.py`
- 删除：`src/insightagent/telemetry/usage.py`
- 修改：`src/insightagent/agent/__init__.py`
- 修改：`src/insightagent/api/__init__.py`
- 修改：`src/insightagent/tools/__init__.py`
- 新建：`tests/test_no_legacy_runtime_imports.py`
- 修改：`docs/superpowers/plans/2026-07-15-langgraph-full-rewrite-implementation.md`

- [x] **步骤 1：写出生产导入静态防护测试。**

```python
import ast
from pathlib import Path


LEGACY_MODULES = {
    "insightagent.agent.core", "insightagent.agent.session", "insightagent.agent.task_contracts",
    "insightagent.api.messages", "insightagent.api.providers", "insightagent.api.resilience",
    "insightagent.tools.registry", "insightagent.telemetry.trace", "insightagent.telemetry.usage",
}


def test_production_runtime_has_no_legacy_runtime_imports() -> None:
    root = Path("src/insightagent")
    hits = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in LEGACY_MODULES:
                hits.append((path, node.module))
            if isinstance(node, ast.Import) and any(alias.name in LEGACY_MODULES for alias in node.names):
                hits.append((path, "import"))
    assert hits == []
    assert all(not (root / module.replace(".", "/")).with_suffix(".py").exists() for module in LEGACY_MODULES)
```

- [x] **步骤 2：确认静态防护在删除前失败。**

运行：`uv run pytest tests/test_no_legacy_runtime_imports.py -q`

预期：列出仍从旧运行时导入的 CLI、评测、测试支持或遥测模块。

- [x] **步骤 3：逐文件迁移或删除旧测试。**

更新测试迁移矩阵，明确列出：

| 旧测试 | 替代测试或处置 |
| --- | --- |
| `test_agent_loop.py`、`test_long_horizon_tasks.py`、`test_small_model_resilience.py`、`test_trajectories.py` | 删除；由 `tests/graph/test_workflow.py`、`tests/graph/test_time_budget.py` 与 `tests/graph/test_tools.py` 覆盖图循环、失败收敛和工具策略。 |
| `test_context.py`、`test_long_task_context_pressure.py`、`test_run_task_prompt.py` | 删除；由 `tests/graph/test_project_memory.py` 和 `tests/graph/test_workflow.py::test_graph_compacts_tool_output_before_the_next_model_call` 覆盖项目记忆、工具结果压缩、下一次模型输入及历史消息裁剪。 |
| `test_session.py`、`test_long_task_session_resume.py` | 删除；由 `tests/graph/test_sessions.py` 与 `tests/graph/test_runner.py` 覆盖 SQLite 检查点、恢复和转录。 |
| `test_task_state.py`、`test_run_verification.py` | 删除；由 `tests/graph/test_workflow.py`、`tests/graph/test_contracts.py` 与 `tests/graph/test_tools.py` 覆盖。 |
| `test_providers.py`、`test_resilience.py` | 删除；由 `tests/graph/test_models.py`、`tests/graph/test_retry.py` 覆盖统一模型配置和图侧重试。 |
| `test_runtime_harness.py`、`test_extended_tools.py`、`test_code_analysis_tools.py`、`test_tool_context.py` | 删除；由 `tests/graph/test_tools.py`（含 `parse_ast`、`get_function_signature`、`find_dependencies`、`get_code_metrics` 的真实 LangChain 工具调用）、`tests/graph/test_contracts.py` 和保留的运行时权限测试覆盖。 |
| `test_trace.py`、`test_slash_commands.py`、`test_mcp_slash_commands.py` | 删除；由 `tests/graph/test_observability.py`、`tests/graph/test_slash_commands.py`、`tests/graph/test_runner.py` 覆盖。 |

保留纯领域测试：配置、权限、命令校验、失败分类、文件/搜索/AST 工具、MCP 协议与传输、SWE-bench 准备和工作区补丁分析。删除没有新框架行为对应物的文本协议、`FakeModelClient` 和旧 JSON 会话测试。

- [x] **步骤 4：删除旧模块与死导出。**

先用 `rg -n "CodeAgent|ModelClient|TaskState|SessionStore|TaskContract|ToolRegistry|UsageTracker|JsonlTraceRecorder|agent\.session|api\.messages|api\.providers" src tests` 逐个修复迁移遗漏，再删除文件。仓库快照迁移到 `graph/repository_snapshot.py`，重试策略迁移到 `graph/retry.py`。静态守卫遍历全部 `src/insightagent`，以 AST 检查直接、别名和 `from ... import ...` 导入，并检查静态字符串形式的 `importlib.import_module()`（含模块别名、函数别名）以及 `__import__()`（含 `builtins` 别名）；同时断言列为删除对象的文件不存在。`tools/__init__.py` 只保留底层领域工具导出，不得导出 `ToolRegistry` 或 `default_tools`。删除旧 `telemetry/trace.py`、`telemetry/usage.py` 和对应旧测试；`GraphDebugRecorder` 与 `UsageAccumulator` 只由 `insightagent.graph` 提供。

- [x] **步骤 5：运行静态防护和完整测试。**

运行：`uv run pytest tests/test_no_legacy_runtime_imports.py -q && uv run pytest -q`

预期：全部通过，生产目录无旧运行时符号。

- [ ] **步骤 6：提交删除与测试迁移。**

```bash
git add -A src tests docs/superpowers/plans/2026-07-15-langgraph-full-rewrite-implementation.md
git commit -m "refactor: remove legacy agent runtime"
```

### 任务 14：更新面向用户的文档并执行发布前验证

**文件：**

- 修改：`README.md`
- 修改：`MCP_GUIDE.md`
- 修改：`docs/superpowers/specs/2026-07-15-langgraph-full-rewrite-design.md`
- 新建：`tests/test_install_smoke.py`

- [x] **步骤 1：写出安装和 CLI 图路径失败测试。**

```python
import subprocess
import sys


def test_installed_cli_uses_graph_runtime(tmp_path) -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "insightagent.cli.run_task", "--no-trace", "--workspace", str(tmp_path), "--task", "创建 hello.py"],
        text=True,
        capture_output=True,
        env={"INSIGHTAGENT_FAKE_MODEL": "write-and-verify"},
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert (tmp_path / "hello.py").is_file()
```

- [x] **步骤 2：确认测试失败。**

运行：`uv run pytest tests/test_install_smoke.py -q`

预期：在尚未加入图 CLI 的伪模型环境开关前失败。

- [x] **步骤 3：记录新运行时行为。**

README 与 MCP 指南须用中文明确：LangGraph 是唯一运行时；会话存储于 `<session-dir>/checkpoints.sqlite3`；`--session-id`、`--checkpoint-id`、`--list-sessions`、`--export-transcript` 的语义；Langfuse 需要 `LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY`、可选 `LANGFUSE_BASE_URL`；`--trace-jsonl` 是调试导出；MCP 工具和内置工具共享权限与超时策略。设计文档如与最终实现存在差异，必须同步为真实接口。

增加仅用于测试的 `INSIGHTAGENT_FAKE_MODEL=write-and-verify` 工厂分支；它只能在 `PYTEST_CURRENT_TEST` 存在时启用，生产环境设置该变量必须抛出配置错误，避免把测试模型暴露为生产后门。

- [x] **步骤 4：执行完整验证。**

运行：

```bash
uv sync --extra dev
uv run python -m compileall src tests
uv run pytest -q
uv run python -c "import langgraph, langchain_core, langfuse"
uv run pytest tests/test_install_smoke.py -q
git diff --check
```

预期：所有命令退出码为 `0`。

- [ ] **步骤 5：提交文档和发布验证。**

```bash
git add README.md MCP_GUIDE.md docs/superpowers/specs/2026-07-15-langgraph-full-rewrite-design.md tests/test_install_smoke.py
git commit -m "docs: document graph runtime migration"
```

## 计划自检

- 设计中的依赖、图状态、模型、工具、MCP、SWE 契约、SQLite 检查点、会话/转录、Langfuse、调试追踪、CLI、斜杠命令、评测、旧运行时删除和文档更新均映射到任务 1 至任务 14。
- 所有新增生产类型和函数在首次使用前已在计划中定义：`AgentState`、`ToolRuntime`、`GraphServices`、`GraphSessionService`、`GraphObservability`、`RunOutcome` 与 `run_task()`。
- 计划没有未落实的占位步骤；每个改动都有确定文件、测试、命令和提交边界。
- 每个任务均先写失败测试，再写最小实现，并给出单独验证命令和提交边界。
