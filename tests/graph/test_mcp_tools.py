from __future__ import annotations

import asyncio
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel

from insightagent.mcp.config import MCPConfig, MCPServerConfig
from insightagent.mcp.manager import MCPManager
from insightagent.runtime.tool_context import ToolContext


def test_manager_exposes_official_langchain_mcp_tool(tmp_path: Path) -> None:
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
        started = await manager.start_enabled()
        status = await manager.status()
        assert started == 1, status["demo"]["last_error"]
        try:
            tools = await manager.get_tools()
            assert all(isinstance(tool, BaseTool) for tool in tools)
            echo = next(tool for tool in tools if tool.name == "mcp_demo_echo")
            result = await echo.ainvoke({"text": "x"})
            assert result["mcp_server"] == "demo"
            assert result["content"][0]["text"] == "echo: x"
        finally:
            await manager.stop_all()

    asyncio.run(scenario())


def test_manager_exposes_resource_and_prompt_tools(tmp_path: Path) -> None:
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
            tools = {tool.name: tool for tool in await manager.get_tools()}
            assert {
                "mcp_demo_list_resources",
                "mcp_demo_read_resource",
                "mcp_demo_list_prompts",
                "mcp_demo_get_prompt",
            } <= set(tools)
            resource = await tools["mcp_demo_read_resource"].ainvoke({"uri": "fake://note"})
            assert resource["content"]["contents"][0]["text"] == "hello resource"
            prompt = await tools["mcp_demo_get_prompt"].ainvoke(
                {"name": "review", "arguments": {}}
            )
            assert prompt["content"]["messages"][0]["content"]["text"] == "review this"
        finally:
            await manager.stop_all()

    asyncio.run(scenario())


class _SlowArguments(BaseModel):
    text: str


class _EmptySession:
    async def list_resources(self):
        return type("Resources", (), {"resources": []})()

    async def list_prompts(self):
        return type("Prompts", (), {"prompts": []})()


class _FakeMCPClient:
    def __init__(self) -> None:
        self.session_entries = 0
        self.session_closures = 0

    @asynccontextmanager
    async def session(self, server_name: str):
        del server_name
        self.session_entries += 1
        try:
            yield _EmptySession()
        finally:
            self.session_closures += 1


def test_mcp_deadline_cancels_active_tool_task(tmp_path: Path) -> None:
    cancelled = asyncio.Event()
    client = _FakeMCPClient()
    call_count = 0

    async def slow_call(text: str) -> str:
        nonlocal call_count
        del text
        call_count += 1
        if call_count == 2:
            return "recovered"
        try:
            async with client.session("demo"):
                await asyncio.sleep(60)
        except asyncio.CancelledError:
            if client.session_entries != client.session_closures:
                (tmp_path / "late-effect").write_text("unsafe", encoding="utf-8")
            cancelled.set()
            raise
        raise AssertionError("slow MCP call returned without cancellation")

    async def fake_loader(session, **kwargs):
        assert session is None
        del kwargs
        tools: list[BaseTool] = [
            StructuredTool(
                name="slow",
                description="slow test MCP tool",
                args_schema=_SlowArguments,
                coroutine=slow_call,
            )
        ]
        return tools

    config = MCPConfig(
        servers={"demo": MCPServerConfig.from_dict("demo", {"command": "unused"})}
    )

    async def scenario() -> None:
        manager = MCPManager(
            config,
            ToolContext(workspace=tmp_path),
            client_factory=lambda connections: client,
            tool_loader=fake_loader,
        )
        assert await manager.start_enabled() == 1
        try:
            tool = next(tool for tool in await manager.get_tools() if tool.name == "mcp_demo_slow")
            closures_before_call = client.session_closures
            result = await tool.ainvoke(
                {"text": "x"},
                config={
                    "configurable": {
                        "insightagent_deadline_monotonic": time.monotonic() + 0.01
                    }
                },
            )
            assert result["failure_kind"] == "time_budget_exceeded"
            assert cancelled.is_set()
            assert client.session_closures == closures_before_call + 1
            assert not (tmp_path / "late-effect").exists()
            assert (await manager.status())["demo"]["active_tasks"] == 0
            unavailable = await tool.ainvoke({"text": "y"})
            assert unavailable["failure_kind"] == "mcp_cancellation_unconfirmed"
            assert await manager.get_tools() == []
            assert await manager.restart_server("demo") is True
            recovered_tool = next(
                tool for tool in await manager.get_tools() if tool.name == "mcp_demo_slow"
            )
            recovered = await recovered_tool.ainvoke({"text": "y"})
            assert recovered["content"] == "recovered"
            assert (await manager.status())["demo"]["state"] == "running"
        finally:
            await manager.stop_all()

    asyncio.run(scenario())


def test_mcp_deadline_reports_unconfirmed_cancellation_when_cleanup_fails(tmp_path: Path) -> None:
    client = _FakeMCPClient()

    async def slow_call(text: str) -> str:
        del text
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError as error:
            raise RuntimeError("session cleanup failed") from error
        raise AssertionError("slow MCP call returned without cancellation")

    async def fake_loader(session, **kwargs):
        assert session is None
        del kwargs
        tools: list[BaseTool] = [
            StructuredTool(
                name="slow",
                description="slow test MCP tool",
                args_schema=_SlowArguments,
                coroutine=slow_call,
            )
        ]
        return tools

    config = MCPConfig(
        servers={"demo": MCPServerConfig.from_dict("demo", {"command": "unused"})}
    )

    async def scenario() -> None:
        manager = MCPManager(
            config,
            ToolContext(workspace=tmp_path),
            client_factory=lambda connections: client,
            tool_loader=fake_loader,
        )
        assert await manager.start_enabled() == 1
        try:
            tool = next(tool for tool in await manager.get_tools() if tool.name == "mcp_demo_slow")
            result = await tool.ainvoke(
                {"text": "x"},
                config={
                    "configurable": {
                        "insightagent_deadline_monotonic": time.monotonic() + 0.01
                    }
                },
            )
            assert result["failure_kind"] == "mcp_cancellation_unconfirmed"
            status = await manager.status()
            assert status["demo"]["state"] == "failed"
            assert status["demo"]["active_tasks"] == 0
        finally:
            await manager.stop_all()

    asyncio.run(scenario())


def test_direct_cancellation_waits_for_deadline_wrapped_mcp_cleanup(tmp_path: Path) -> None:
    client = _FakeMCPClient()
    started = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()

    async def slow_call(text: str) -> str:
        del text
        try:
            async with client.session("demo"):
                started.set()
                await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return "finished"

    async def fake_loader(session, **kwargs):
        assert session is None
        del kwargs
        tools: list[BaseTool] = [
            StructuredTool(
                name="slow",
                description="slow test MCP tool",
                args_schema=_SlowArguments,
                coroutine=slow_call,
            )
        ]
        return tools

    config = MCPConfig(
        servers={"demo": MCPServerConfig.from_dict("demo", {"command": "unused"})}
    )

    async def scenario() -> None:
        manager = MCPManager(
            config,
            ToolContext(workspace=tmp_path),
            client_factory=lambda connections: client,
            tool_loader=fake_loader,
        )
        assert await manager.start_enabled() == 1
        try:
            tool = next(tool for tool in await manager.get_tools() if tool.name == "mcp_demo_slow")
            closures_before_call = client.session_closures
            invocation = asyncio.create_task(
                tool.ainvoke(
                    {"text": "x"},
                    config={
                        "configurable": {
                            "insightagent_deadline_monotonic": time.monotonic() + 60
                        }
                    },
                )
            )
            await asyncio.wait_for(started.wait(), timeout=0.5)
            invocation.cancel()
            result = (await asyncio.gather(invocation, return_exceptions=True))[0]
            assert isinstance(result, asyncio.CancelledError)
            assert cancelled.is_set()
            assert client.session_closures == closures_before_call + 1
            assert (await manager.status())["demo"]["active_tasks"] == 0
        finally:
            release.set()
            await asyncio.sleep(0)
            await manager.stop_all()

    asyncio.run(scenario())


def test_remote_timeout_is_not_reclassified_as_turn_budget_exhaustion(tmp_path: Path) -> None:
    client = _FakeMCPClient()

    async def remote_timeout(text: str) -> str:
        del text
        raise asyncio.TimeoutError("remote deadline")

    async def fake_loader(session, **kwargs):
        assert session is None
        del kwargs
        tools: list[BaseTool] = [
            StructuredTool(
                name="remote_timeout",
                description="remote timeout test MCP tool",
                args_schema=_SlowArguments,
                coroutine=remote_timeout,
            )
        ]
        return tools

    config = MCPConfig(
        servers={"demo": MCPServerConfig.from_dict("demo", {"command": "unused"})}
    )

    async def scenario() -> None:
        manager = MCPManager(
            config,
            ToolContext(workspace=tmp_path),
            client_factory=lambda connections: client,
            tool_loader=fake_loader,
        )
        assert await manager.start_enabled() == 1
        try:
            tool = next(
                tool for tool in await manager.get_tools() if tool.name == "mcp_demo_remote_timeout"
            )
            result = await tool.ainvoke(
                {"text": "x"},
                config={
                    "configurable": {
                        "insightagent_deadline_monotonic": time.monotonic() + 60
                    }
                },
            )
            assert result["failure_kind"] == "unknown_error"
            assert result["failure_kind"] != "time_budget_exceeded"
            assert (await manager.status())["demo"]["state"] == "running"
        finally:
            await manager.stop_all()

    asyncio.run(scenario())


def test_repeated_cancellation_keeps_timed_out_mcp_cleanup_managed(tmp_path: Path) -> None:
    client = _FakeMCPClient()
    cleanup_started = asyncio.Event()
    cleanup_finished = asyncio.Event()
    release_cleanup = asyncio.Event()

    async def slow_call(text: str) -> str:
        del text
        async with client.session("demo"):
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cleanup_started.set()
                await release_cleanup.wait()
                raise
            finally:
                cleanup_finished.set()
        return "finished"

    async def fake_loader(session, **kwargs):
        assert session is None
        del kwargs
        tools: list[BaseTool] = [
            StructuredTool(
                name="slow",
                description="slow test MCP tool",
                args_schema=_SlowArguments,
                coroutine=slow_call,
            )
        ]
        return tools

    config = MCPConfig(
        servers={"demo": MCPServerConfig.from_dict("demo", {"command": "unused"})}
    )

    async def scenario() -> None:
        manager = MCPManager(
            config,
            ToolContext(workspace=tmp_path),
            client_factory=lambda connections: client,
            tool_loader=fake_loader,
        )
        assert await manager.start_enabled() == 1
        try:
            tool = next(tool for tool in await manager.get_tools() if tool.name == "mcp_demo_slow")
            invocation = asyncio.create_task(
                tool.ainvoke(
                    {"text": "x"},
                    config={
                        "configurable": {
                            "insightagent_deadline_monotonic": time.monotonic() + 0.01
                        }
                    },
                )
            )
            await asyncio.wait_for(cleanup_started.wait(), timeout=0.5)
            invocation.cancel()
            await asyncio.sleep(0)
            assert not invocation.done()
            assert (await manager.status())["demo"]["active_tasks"] == 1
            release_cleanup.set()
            await asyncio.wait_for(cleanup_finished.wait(), timeout=0.5)
            result = (await asyncio.gather(invocation, return_exceptions=True))[0]
            assert isinstance(result, asyncio.CancelledError)
            assert (await manager.status())["demo"]["active_tasks"] == 0
        finally:
            release_cleanup.set()
            await manager.stop_all()

    asyncio.run(scenario())
