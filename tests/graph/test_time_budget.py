from __future__ import annotations

import asyncio
import errno
import json
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, ConfigDict
import pytest

from insightagent.graph.tools import ContractAwareToolInvoker, ToolRuntime, build_builtin_tools
from insightagent.mcp.config import MCPConfig, MCPServerConfig
from insightagent.mcp.manager import MCPManager
from insightagent.runtime.tool_context import ToolContext
from insightagent.runtime.types import ToolPermission, ToolRisk, ToolSpec
from tests.graph.fakes import ScriptedRunnable


class _SlowRunnable(ScriptedRunnable):
    def __init__(self) -> None:
        super().__init__([AIMessage(content="不应完成")])
        self.cancelled = asyncio.Event()

    async def ainvoke(self, input, config=None, **kwargs):
        del input, config, kwargs
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        raise AssertionError("slow model returned without cancellation")


class _NoArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _TextArguments(BaseModel):
    text: str


class _EmptyMCPServerSession:
    async def list_resources(self):
        return type("Resources", (), {"resources": []})()

    async def list_prompts(self):
        return type("Prompts", (), {"prompts": []})()


class _TrackingMCPClient:
    def __init__(self) -> None:
        self.session_entries = 0
        self.session_closures = 0

    @asynccontextmanager
    async def session(self, server_name: str):
        del server_name
        self.session_entries += 1
        try:
            yield _EmptyMCPServerSession()
        finally:
            self.session_closures += 1


def _slow_tool_spec(name: str, permission: ToolPermission) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=name,
        input_schema={},
        required_permission=permission,
        risk=ToolRisk.LOW,
        mcp_server="fake" if permission is ToolPermission.MCP else None,
    )


def test_graph_cancels_slow_model_when_turn_budget_expires(tmp_path: Path) -> None:
    from insightagent.graph.workflow import GraphServices, build_graph

    model = _SlowRunnable()
    context = ToolContext(workspace=tmp_path)
    services = GraphServices(
        model=model,
        tools={},
        tool_specs={},
        tool_context=context,
        tool_invoker=ContractAwareToolInvoker(context),
        max_iterations=2,
        max_repair_attempts=1,
    )
    graph = build_graph(services)

    started = time.monotonic()
    result = asyncio.run(
        graph.ainvoke(
            services.initial_state("分析仓库"),
            {
                "configurable": {
                    "thread_id": "timeout-1",
                    "insightagent_deadline_monotonic": time.monotonic() + 0.05,
                }
            },
        )
    )

    assert time.monotonic() - started < 1
    assert result["phase"] == "failed"
    assert result["tool_events"][-1]["failure_kind"] == "time_budget_exceeded"
    assert model.cancelled.is_set()


@pytest.mark.parametrize(
    ("name", "permission"),
    [("slow_tool", ToolPermission.READ), ("slow_mcp_tool", ToolPermission.MCP)],
)
def test_graph_cancels_slow_tool_when_turn_budget_expires(
    tmp_path: Path, name: str, permission: ToolPermission
) -> None:
    from insightagent.graph.workflow import GraphServices, build_graph

    cancelled = asyncio.Event()

    async def slow_tool() -> dict[str, object]:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        raise AssertionError("slow tool returned without cancellation")

    tool = StructuredTool(
        name=name,
        description="A slow tool.",
        args_schema=_NoArguments,
        coroutine=slow_tool,
    )
    context = ToolContext(workspace=tmp_path)
    services = GraphServices(
        model=ScriptedRunnable(
            [
                AIMessage(
                    content="",
                    tool_calls=[{"name": name, "args": {}, "id": "slow-1"}],
                )
            ]
        ),
        tools={tool.name: tool},
        tool_specs={tool.name: _slow_tool_spec(tool.name, permission)},
        tool_context=context,
        tool_invoker=ContractAwareToolInvoker(context),
    )

    started = time.monotonic()
    result = asyncio.run(
        build_graph(services).ainvoke(
            services.initial_state("分析仓库"),
            {
                "configurable": {
                    "thread_id": "slow-tool-timeout",
                    "insightagent_deadline_monotonic": time.monotonic() + 0.05,
                }
            },
        )
    )

    assert time.monotonic() - started < 1
    assert result["phase"] == "failed"
    assert result["tool_events"][-1]["failure_kind"] == "time_budget_exceeded"
    assert cancelled.is_set()


def test_graph_completes_tool_batch_after_the_first_call_exhausts_the_budget(
    tmp_path: Path,
) -> None:
    from insightagent.graph.workflow import GraphServices, build_graph

    cancelled = asyncio.Event()
    second_call_count = 0

    async def slow_tool() -> dict[str, object]:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        raise AssertionError("slow tool returned without cancellation")

    async def second_tool() -> dict[str, object]:
        nonlocal second_call_count
        second_call_count += 1
        return {"is_error": False, "content": "should not run"}

    slow = StructuredTool(
        name="slow_tool",
        description="A slow tool.",
        args_schema=_NoArguments,
        coroutine=slow_tool,
    )
    second = StructuredTool(
        name="second_tool",
        description="A tool after the slow call.",
        args_schema=_NoArguments,
        coroutine=second_tool,
    )
    context = ToolContext(workspace=tmp_path)
    services = GraphServices(
        model=ScriptedRunnable(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": slow.name, "args": {}, "id": "call-1"},
                        {"name": second.name, "args": {}, "id": "call-2"},
                    ],
                )
            ]
        ),
        tools={slow.name: slow, second.name: second},
        tool_specs={
            slow.name: _slow_tool_spec(slow.name, ToolPermission.READ),
            second.name: _slow_tool_spec(second.name, ToolPermission.READ),
        },
        tool_context=context,
        tool_invoker=ContractAwareToolInvoker(context),
    )

    result = asyncio.run(
        build_graph(services).ainvoke(
            services.initial_state("分析仓库"),
            {
                "configurable": {
                    "thread_id": "slow-tool-batch-timeout",
                    "insightagent_deadline_monotonic": time.monotonic() + 0.05,
                }
            },
        )
    )

    tool_messages = [
        message for message in result["messages"] if isinstance(message, ToolMessage)
    ]

    assert result["phase"] == "failed"
    assert cancelled.is_set()
    assert second_call_count == 0
    assert [message.tool_call_id for message in tool_messages] == ["call-1", "call-2"]
    assert [json.loads(message.content)["failure_kind"] for message in tool_messages] == [
        "time_budget_exceeded",
        "tool_batch_aborted",
    ]


def test_graph_reclaims_shell_process_tree_when_turn_budget_expires(tmp_path: Path) -> None:
    from insightagent.graph.workflow import GraphServices, build_graph

    pids_path = tmp_path.parent / f"{tmp_path.name}-shell-pids"
    child_code = "import time; time.sleep(30)"
    parent_code = (
        "import os, pathlib, subprocess, sys, time; "
        f"child = subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        f"pathlib.Path({str(pids_path)!r}).write_text(str(os.getpid()) + ' ' + str(child.pid)); "
        "time.sleep(30)"
    )
    context = ToolContext(workspace=tmp_path)
    runtime = ToolRuntime(context)
    tools = build_builtin_tools(context, runtime)
    execute = next(tool for tool in tools if tool.name == "execute_command")
    services = GraphServices(
        model=ScriptedRunnable(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "execute_command",
                            "args": {"command": f"{sys.executable} -c {parent_code!r}", "timeout": 30},
                            "id": "shell-1",
                        }
                    ],
                )
            ]
        ),
        tools={execute.name: execute},
        tool_specs={execute.name: runtime.specs()[execute.name]},
        tool_context=context,
        tool_invoker=ContractAwareToolInvoker(context),
    )

    try:
        result = asyncio.run(
            build_graph(services).ainvoke(
                services.initial_state("执行受时间预算限制的诊断命令。"),
                {
                    "configurable": {
                        "thread_id": "shell-tree-timeout",
                        "insightagent_deadline_monotonic": time.monotonic() + 0.5,
                    }
                },
            )
        )

        assert result["phase"] == "failed"
        assert result["tool_events"][-1]["failure_kind"] == "time_budget_exceeded"
        assert pids_path.is_file(), result["tool_events"][-1]["content"]
        parent_pid, child_pid = _wait_for_pids(pids_path)
        assert _process_eventually_gone(parent_pid)
        assert _process_eventually_gone(child_pid)
    finally:
        if pids_path.is_file():
            for pid in _wait_for_pids(pids_path):
                _kill_if_running(pid)
            pids_path.unlink()


def test_graph_waits_for_mcp_session_cleanup_when_turn_budget_expires(tmp_path: Path) -> None:
    from insightagent.graph.workflow import GraphServices, build_graph

    client = _TrackingMCPClient()
    cancelled = asyncio.Event()

    async def slow_call(text: str) -> str:
        del text
        try:
            async with client.session("demo"):
                await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        raise AssertionError("slow MCP call returned without cancellation")

    async def fake_loader(session: object, **kwargs: object) -> list[BaseTool]:
        assert session is None
        del kwargs
        return [
            StructuredTool(
                name="slow",
                description="A slow MCP tool.",
                args_schema=_TextArguments,
                coroutine=slow_call,
            )
        ]

    config = MCPConfig(
        servers={"demo": MCPServerConfig.from_dict("demo", {"command": "unused"})}
    )

    async def scenario() -> None:
        context = ToolContext(workspace=tmp_path)
        manager = MCPManager(
            config,
            context,
            client_factory=lambda connections: client,
            tool_loader=fake_loader,
        )
        assert await manager.start_enabled() == 1
        try:
            tools = await manager.get_tools()
            specs = await manager.get_tool_specs()
            mcp_tool = next(tool for tool in tools if tool.name == "mcp_demo_slow")
            closures_before_call = client.session_closures
            services = GraphServices(
                model=ScriptedRunnable(
                    [
                        AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": mcp_tool.name,
                                    "args": {"text": "x"},
                                    "id": "mcp-1",
                                }
                            ],
                        )
                    ]
                ),
                tools={mcp_tool.name: mcp_tool},
                tool_specs={mcp_tool.name: specs[mcp_tool.name]},
                tool_context=context,
                tool_invoker=ContractAwareToolInvoker(context),
            )

            result = await build_graph(services).ainvoke(
                services.initial_state("调用 MCP 工具。"),
                {
                    "configurable": {
                        "thread_id": "mcp-cleanup-timeout",
                        "insightagent_deadline_monotonic": time.monotonic() + 0.05,
                    }
                },
            )

            assert result["phase"] == "failed"
            assert result["tool_events"][-1]["failure_kind"] == "time_budget_exceeded"
            assert cancelled.is_set()
            assert client.session_closures == closures_before_call + 1
            assert (await manager.status())["demo"]["active_tasks"] == 0
        finally:
            await manager.stop_all()

    asyncio.run(scenario())


def _process_is_gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError as error:
        if error.errno == errno.ESRCH:
            return True
        raise
    return False


def _kill_if_running(pid: int) -> None:
    if not _process_is_gone(pid):
        os.kill(pid, 9)
        for _ in range(100):
            if _process_is_gone(pid):
                return
            time.sleep(0.01)


def _wait_for_pids(path: Path) -> tuple[int, ...]:
    for _ in range(100):
        if path.is_file():
            return tuple(int(pid) for pid in path.read_text(encoding="utf-8").split())
        time.sleep(0.01)
    raise AssertionError(f"process did not write pid file: {path}")


def _process_eventually_gone(pid: int) -> bool:
    for _ in range(100):
        if _process_is_gone(pid):
            return True
        time.sleep(0.01)
    return False
