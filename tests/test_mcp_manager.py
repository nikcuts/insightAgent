from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import time

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel

from insightagent.mcp.config import MCPConfig, MCPServerConfig
from insightagent.mcp.manager import MCPManager
from insightagent.runtime.tool_context import ToolContext


class NoArguments(BaseModel):
    pass


class FakeSession:
    async def list_resources(self):
        return type("Resources", (), {"resources": []})()

    async def list_prompts(self):
        return type("Prompts", (), {"prompts": []})()


class FakeClient:
    @asynccontextmanager
    async def session(self, server_name: str):
        del server_name
        yield FakeSession()


class PromptOnlySession:
    async def list_resources(self):
        raise RuntimeError("resources are not supported")

    async def list_prompts(self):
        prompt = type("Prompt", (), {"name": "review", "description": "Review a change."})()
        return type("Prompts", (), {"prompts": [prompt]})()


class PromptOnlyClient:
    @asynccontextmanager
    async def session(self, server_name: str):
        del server_name
        yield PromptOnlySession()


class PaginatedSession:
    async def list_resources(self, cursor: str | None = None):
        if cursor is None:
            return type("Resources", (), {"resources": ["one"], "nextCursor": "resources-2"})()
        assert cursor == "resources-2"
        return type("Resources", (), {"resources": ["two"], "nextCursor": None})()

    async def list_prompts(self, cursor: str | None = None):
        if cursor is None:
            return type("Prompts", (), {"prompts": ["review"], "nextCursor": "prompts-2"})()
        assert cursor == "prompts-2"
        return type("Prompts", (), {"prompts": ["summarize"], "nextCursor": None})()


class PaginatedClient:
    @asynccontextmanager
    async def session(self, server_name: str):
        del server_name
        yield PaginatedSession()


def test_manager_keeps_prompts_when_resources_are_unsupported(tmp_path: Path) -> None:
    config = MCPConfig(
        servers={"demo": MCPServerConfig.from_dict("demo", {"command": "demo"})}
    )

    async def loader(session, **kwargs):
        assert session is None
        del kwargs
        return []

    async def scenario() -> None:
        manager = MCPManager(
            config,
            ToolContext(workspace=tmp_path),
            client_factory=lambda connections: PromptOnlyClient(),
            tool_loader=loader,
        )
        assert await manager.start_enabled() == 1
        try:
            assert [tool.name for tool in await manager.get_tools()] == [
                "mcp_demo_list_prompts",
                "mcp_demo_get_prompt",
            ]
            status = await manager.status()
            assert status["demo"]["resources"] == 0
            assert status["demo"]["prompts"] == 1
        finally:
            await manager.stop_all()

    asyncio.run(scenario())


def test_manager_exposes_all_paginated_resource_and_prompt_items(tmp_path: Path) -> None:
    config = MCPConfig(
        servers={"demo": MCPServerConfig.from_dict("demo", {"command": "demo"})}
    )

    async def loader(session, **kwargs):
        assert session is None
        del kwargs
        return []

    async def scenario() -> None:
        manager = MCPManager(
            config,
            ToolContext(workspace=tmp_path),
            client_factory=lambda connections: PaginatedClient(),
            tool_loader=loader,
        )
        assert await manager.start_enabled() == 1
        try:
            tools = {tool.name: tool for tool in await manager.get_tools()}
            resources = await tools["mcp_demo_list_resources"].ainvoke({})
            prompts = await tools["mcp_demo_list_prompts"].ainvoke({})
            assert resources["content"] == {"resources": ["one", "two"]}
            assert prompts["content"] == {"prompts": ["review", "summarize"]}
            status = await manager.status()
            assert status["demo"]["resources"] == 2
            assert status["demo"]["prompts"] == 2
        finally:
            await manager.stop_all()

    asyncio.run(scenario())


def test_async_manager_isolates_startup_failure_and_reports_duplicate_tools(tmp_path: Path) -> None:
    config = MCPConfig(
        servers={
            "one": MCPServerConfig.from_dict("one", {"command": "one", "tool_prefix": "mcp_same"}),
            "two": MCPServerConfig.from_dict("two", {"command": "two", "tool_prefix": "mcp_same"}),
            "bad": MCPServerConfig.from_dict("bad", {"command": "bad"}),
        }
    )

    async def loader(session, *, server_name, **kwargs):
        assert session is None
        del kwargs
        if server_name == "bad":
            raise RuntimeError("boom")
        tools: list[BaseTool] = [
            StructuredTool(
                name="echo",
                description="Echo.",
                args_schema=NoArguments,
                coroutine=lambda: asyncio.sleep(0, result="ok"),
                metadata={"readOnlyHint": True},
            )
        ]
        return tools

    async def scenario() -> None:
        manager = MCPManager(
            config,
            ToolContext(workspace=tmp_path),
            client_factory=lambda connections: FakeClient(),
            tool_loader=loader,
        )
        assert await manager.start_enabled() == 2
        try:
            assert [tool.name for tool in await manager.get_tools()] == ["mcp_same_echo"]
            status = await manager.status()
            assert "duplicate tool name" in status["two"]["last_error"]
            assert "boom" in status["bad"]["last_error"]
            assert await manager.restart_server("one") is True
            assert await manager.refresh_server("one") is True
        finally:
            await manager.stop_all()

    asyncio.run(scenario())


def test_manager_serializes_concurrent_start_and_restart(tmp_path: Path) -> None:
    config = MCPConfig(
        servers={"demo": MCPServerConfig.from_dict("demo", {"command": "demo"})}
    )
    active_openings = 0
    max_active_openings = 0

    async def loader(session, **kwargs):
        nonlocal active_openings, max_active_openings
        assert session is None
        del kwargs
        active_openings += 1
        max_active_openings = max(max_active_openings, active_openings)
        try:
            await asyncio.sleep(0.05)
        finally:
            active_openings -= 1
        return []

    async def scenario() -> None:
        manager = MCPManager(
            config,
            ToolContext(workspace=tmp_path),
            client_factory=lambda connections: FakeClient(),
            tool_loader=loader,
        )
        try:
            assert await asyncio.gather(manager.start_enabled(), manager.restart_server("demo")) == [1, True]
            assert max_active_openings == 1
            assert (await manager.status())["demo"]["state"] == "running"
        finally:
            await manager.stop_all()

    asyncio.run(scenario())


def test_restart_cancels_running_call_before_queued_call_can_start(tmp_path: Path) -> None:
    config = MCPConfig(
        servers={"demo": MCPServerConfig.from_dict("demo", {"command": "demo"})}
    )
    started = asyncio.Event()
    release = asyncio.Event()
    cancelled = asyncio.Event()
    call_count = 0

    async def slow_call() -> str:
        nonlocal call_count
        call_count += 1
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return "finished"

    async def loader(session, **kwargs):
        assert session is None
        del kwargs
        tools: list[BaseTool] = [
            StructuredTool(
                name="slow",
                description="Slow call.",
                args_schema=NoArguments,
                coroutine=slow_call,
                metadata={"readOnlyHint": True},
            )
        ]
        return tools

    async def scenario() -> None:
        manager = MCPManager(
            config,
            ToolContext(workspace=tmp_path),
            client_factory=lambda connections: FakeClient(),
            tool_loader=loader,
        )
        assert await manager.start_enabled() == 1
        first: asyncio.Task[object] | None = None
        second: asyncio.Task[object] | None = None
        try:
            tool = next(tool for tool in await manager.get_tools() if tool.name == "mcp_demo_slow")
            first = asyncio.create_task(tool.ainvoke({}))
            await asyncio.wait_for(started.wait(), timeout=0.5)
            second = asyncio.create_task(tool.ainvoke({}))
            await asyncio.sleep(0)
            assert await asyncio.wait_for(manager.restart_server("demo"), timeout=0.5) is True
            first_result, second_result = await asyncio.gather(first, second, return_exceptions=True)
            assert isinstance(first_result, asyncio.CancelledError)
            assert isinstance(second_result, dict)
            assert second_result["failure_kind"] == "mcp_cancellation_unconfirmed"
            assert cancelled.is_set()
            assert call_count == 1
            assert (await manager.status())["demo"]["active_tasks"] == 0
        finally:
            release.set()
            if first is not None:
                await asyncio.gather(first, return_exceptions=True)
            if second is not None:
                await asyncio.gather(second, return_exceptions=True)
            await manager.stop_all()

    asyncio.run(scenario())


def test_timed_out_mcp_call_waits_for_cleanup_and_disables_the_server(tmp_path: Path) -> None:
    config = MCPConfig(
        servers={"demo": MCPServerConfig.from_dict("demo", {"command": "demo"})}
    )
    started = asyncio.Event()
    cancellation_started = asyncio.Event()
    allow_cleanup = asyncio.Event()
    cleanup_finished = asyncio.Event()
    call_count = 0

    async def slow_call() -> str:
        nonlocal call_count
        call_count += 1
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancellation_started.set()
            await allow_cleanup.wait()
            cleanup_finished.set()
            raise
        raise AssertionError("slow MCP call returned without cancellation")

    async def loader(session, **kwargs):
        assert session is None
        del kwargs
        return [
            StructuredTool(
                name="slow",
                description="Slow call.",
                args_schema=NoArguments,
                coroutine=slow_call,
                metadata={"readOnlyHint": True},
            )
        ]

    async def scenario() -> None:
        manager = MCPManager(
            config,
            ToolContext(workspace=tmp_path),
            client_factory=lambda connections: FakeClient(),
            tool_loader=loader,
        )
        assert await manager.start_enabled() == 1
        try:
            tool = next(tool for tool in await manager.get_tools() if tool.name == "mcp_demo_slow")
            timed_out_call = asyncio.create_task(
                tool.ainvoke(
                    {},
                    config={
                        "configurable": {
                            "insightagent_deadline_monotonic": time.monotonic() + 0.05,
                        }
                    },
                )
            )
            await asyncio.wait_for(started.wait(), timeout=0.5)
            await asyncio.wait_for(cancellation_started.wait(), timeout=0.5)
            assert not timed_out_call.done()

            allow_cleanup.set()
            result = await asyncio.wait_for(timed_out_call, timeout=0.5)

            assert result["failure_kind"] == "time_budget_exceeded"
            assert (await manager.status())["demo"]["state"] == "failed"
            unavailable = await tool.ainvoke({})
            assert unavailable["failure_kind"] == "mcp_cancellation_unconfirmed"
            assert call_count == 1
        finally:
            allow_cleanup.set()
            await manager.stop_all()

    asyncio.run(scenario())


def test_mcp_call_waits_through_repeated_cancellation_before_propagating(tmp_path: Path) -> None:
    config = MCPConfig(
        servers={"demo": MCPServerConfig.from_dict("demo", {"command": "demo"})}
    )
    started = asyncio.Event()
    cancellation_started = asyncio.Event()
    allow_cleanup = asyncio.Event()
    cleanup_finished = asyncio.Event()

    async def slow_call() -> str:
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancellation_started.set()
            await allow_cleanup.wait()
            cleanup_finished.set()
            raise
        raise AssertionError("slow MCP call returned without cancellation")

    async def loader(session, **kwargs):
        assert session is None
        del kwargs
        return [
            StructuredTool(
                name="slow",
                description="Slow call.",
                args_schema=NoArguments,
                coroutine=slow_call,
                metadata={"readOnlyHint": True},
            )
        ]

    async def scenario() -> None:
        manager = MCPManager(
            config,
            ToolContext(workspace=tmp_path),
            client_factory=lambda connections: FakeClient(),
            tool_loader=loader,
        )
        assert await manager.start_enabled() == 1
        try:
            tool = next(tool for tool in await manager.get_tools() if tool.name == "mcp_demo_slow")
            cancelled_call = asyncio.create_task(tool.ainvoke({}))
            await asyncio.wait_for(started.wait(), timeout=0.5)
            cancelled_call.cancel()
            await asyncio.wait_for(cancellation_started.wait(), timeout=0.5)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            cancelled_call.cancel()
            cancelled_call.cancel()
            await asyncio.sleep(0)
            assert not cancelled_call.done()

            allow_cleanup.set()
            result = await asyncio.wait_for(
                asyncio.gather(cancelled_call, return_exceptions=True), timeout=0.5
            )
            assert isinstance(result[0], asyncio.CancelledError)
            assert cleanup_finished.is_set()

            assert (await manager.status())["demo"]["state"] == "failed"
            unavailable = await tool.ainvoke({})
            assert unavailable["failure_kind"] == "mcp_cancellation_unconfirmed"
        finally:
            allow_cleanup.set()
            await manager.stop_all()

    asyncio.run(scenario())


def test_cancelled_restart_waits_for_session_cleanup_and_removes_old_tools(tmp_path: Path) -> None:
    config = MCPConfig(
        servers={"demo": MCPServerConfig.from_dict("demo", {"command": "demo"})}
    )
    started = asyncio.Event()
    cleanup_started = asyncio.Event()
    allow_cleanup = asyncio.Event()
    cleanup_finished = asyncio.Event()

    async def slow_call() -> str:
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cleanup_started.set()
            await allow_cleanup.wait()
            cleanup_finished.set()
            raise
        raise AssertionError("slow MCP call returned without cancellation")

    async def loader(session, **kwargs):
        assert session is None
        del kwargs
        return [
            StructuredTool(
                name="slow",
                description="Slow call.",
                args_schema=NoArguments,
                coroutine=slow_call,
                metadata={"readOnlyHint": True},
            )
        ]

    async def scenario() -> None:
        manager = MCPManager(
            config,
            ToolContext(workspace=tmp_path),
            client_factory=lambda connections: FakeClient(),
            tool_loader=loader,
        )
        assert await manager.start_enabled() == 1
        invocation: asyncio.Task[object] | None = None
        restart: asyncio.Task[bool] | None = None
        try:
            tool = next(tool for tool in await manager.get_tools() if tool.name == "mcp_demo_slow")
            invocation = asyncio.create_task(tool.ainvoke({}))
            await asyncio.wait_for(started.wait(), timeout=0.5)
            restart = asyncio.create_task(manager.restart_server("demo"))
            await asyncio.wait_for(cleanup_started.wait(), timeout=0.5)
            restart.cancel()
            await asyncio.sleep(0.01)
            assert not restart.done()
            assert not cleanup_finished.is_set()

            allow_cleanup.set()
            restart_result = (await asyncio.gather(restart, return_exceptions=True))[0]
            assert isinstance(restart_result, asyncio.CancelledError)
            invocation_result = (await asyncio.gather(invocation, return_exceptions=True))[0]
            assert isinstance(invocation_result, asyncio.CancelledError)
            assert cleanup_finished.is_set()
            assert await manager.get_tools() == []
            assert "demo" not in manager.clients
            assert (await manager.status())["demo"]["state"] == "stopped"
        finally:
            allow_cleanup.set()
            if restart is not None:
                await asyncio.gather(restart, return_exceptions=True)
            if invocation is not None:
                await asyncio.gather(invocation, return_exceptions=True)
            await manager.stop_all()

    asyncio.run(scenario())


def test_cancelled_stop_all_waits_for_every_server_cleanup(tmp_path: Path) -> None:
    config = MCPConfig(
        servers={
            "alpha": MCPServerConfig.from_dict("alpha", {"command": "alpha"}),
            "beta": MCPServerConfig.from_dict("beta", {"command": "beta"}),
        }
    )
    started = {name: asyncio.Event() for name in config.servers}
    cleanup_started = {name: asyncio.Event() for name in config.servers}
    allow_cleanup = {name: asyncio.Event() for name in config.servers}
    cleanup_finished = {name: asyncio.Event() for name in config.servers}

    def slow_call(name: str):
        async def call() -> str:
            started[name].set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cleanup_started[name].set()
                await allow_cleanup[name].wait()
                cleanup_finished[name].set()
                raise
            raise AssertionError("slow MCP call returned without cancellation")

        return call

    async def loader(session, **kwargs):
        assert session is None
        name = kwargs["server_name"]
        return [
            StructuredTool(
                name="slow",
                description="Slow call.",
                args_schema=NoArguments,
                coroutine=slow_call(name),
                metadata={"readOnlyHint": True},
            )
        ]

    async def scenario() -> None:
        manager = MCPManager(
            config,
            ToolContext(workspace=tmp_path),
            client_factory=lambda connections: FakeClient(),
            tool_loader=loader,
        )
        assert await manager.start_enabled() == 2
        invocations: list[asyncio.Task[object]] = []
        stop: asyncio.Task[None] | None = None
        try:
            tools = {tool.name: tool for tool in await manager.get_tools()}
            for name in config.servers:
                invocations.append(asyncio.create_task(tools[f"mcp_{name}_slow"].ainvoke({})))
            await asyncio.wait_for(
                asyncio.gather(*(event.wait() for event in started.values())), timeout=0.5
            )

            leases = dict(manager._leases)
            stop = asyncio.create_task(manager.stop_all())
            await asyncio.wait_for(cleanup_started["alpha"].wait(), timeout=0.5)
            for _ in range(3):
                stop.cancel()
                await asyncio.sleep(0)
                assert not stop.done()

            allow_cleanup["alpha"].set()
            await asyncio.wait_for(cleanup_started["beta"].wait(), timeout=0.5)
            for _ in range(2):
                stop.cancel()
                await asyncio.sleep(0)
                assert not stop.done()
            allow_cleanup["beta"].set()

            stop_result = (await asyncio.gather(stop, return_exceptions=True))[0]
            assert isinstance(stop_result, asyncio.CancelledError)
            invocation_results = await asyncio.gather(*invocations, return_exceptions=True)
            assert all(isinstance(result, asyncio.CancelledError) for result in invocation_results)
            assert all(event.is_set() for event in cleanup_finished.values())
            assert all(lease.state == "stopped" for lease in leases.values())
            assert all(not lease.tasks for lease in leases.values())
            assert manager.clients == {}
            assert await manager.get_tools() == []
            assert all(item["state"] == "stopped" for item in (await manager.status()).values())
        finally:
            for event in allow_cleanup.values():
                event.set()
            if stop is not None:
                await asyncio.gather(stop, return_exceptions=True)
            await manager.stop_all()
            await asyncio.gather(*invocations, return_exceptions=True)

    asyncio.run(scenario())


def test_cancelled_stop_all_waiting_for_lifecycle_lock_still_cleans_up(tmp_path: Path) -> None:
    config = MCPConfig(
        servers={"demo": MCPServerConfig.from_dict("demo", {"command": "demo"})}
    )
    restart_opened = asyncio.Event()
    allow_restart = asyncio.Event()
    loader_calls = 0

    async def loader(session, **kwargs):
        nonlocal loader_calls
        assert session is None
        del kwargs
        loader_calls += 1
        if loader_calls == 2:
            restart_opened.set()
            await allow_restart.wait()
        return []

    async def scenario() -> None:
        manager = MCPManager(
            config,
            ToolContext(workspace=tmp_path),
            client_factory=lambda connections: FakeClient(),
            tool_loader=loader,
        )
        assert await manager.start_enabled() == 1
        restart: asyncio.Task[bool] | None = None
        stop: asyncio.Task[None] | None = None
        try:
            restart = asyncio.create_task(manager.restart_server("demo"))
            await asyncio.wait_for(restart_opened.wait(), timeout=0.5)
            stop = asyncio.create_task(manager.stop_all())
            await asyncio.sleep(0)
            stop.cancel()
            await asyncio.sleep(0)
            assert not stop.done()

            allow_restart.set()
            assert await asyncio.wait_for(restart, timeout=0.5) is True
            stop_result = (await asyncio.gather(stop, return_exceptions=True))[0]
            assert isinstance(stop_result, asyncio.CancelledError)
            assert (await manager.status())["demo"]["state"] == "stopped"
            assert manager.clients == {}
            assert await manager.get_tools() == []
        finally:
            allow_restart.set()
            if restart is not None:
                await asyncio.gather(restart, return_exceptions=True)
            if stop is not None:
                await asyncio.gather(stop, return_exceptions=True)
            await manager.stop_all()

    asyncio.run(scenario())


def test_mcp_timeout_finishes_disabling_the_server_before_late_cancellation(tmp_path: Path) -> None:
    config = MCPConfig(
        servers={"demo": MCPServerConfig.from_dict("demo", {"command": "demo"})}
    )
    started = asyncio.Event()
    disable_started = asyncio.Event()
    allow_disable = asyncio.Event()

    async def slow_call() -> str:
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            raise
        raise AssertionError("slow MCP call returned without cancellation")

    async def loader(session, **kwargs):
        assert session is None
        del kwargs
        return [
            StructuredTool(
                name="slow",
                description="Slow call.",
                args_schema=NoArguments,
                coroutine=slow_call,
                metadata={"readOnlyHint": True},
            )
        ]

    async def scenario() -> None:
        manager = MCPManager(
            config,
            ToolContext(workspace=tmp_path),
            client_factory=lambda connections: FakeClient(),
            tool_loader=loader,
        )
        assert await manager.start_enabled() == 1
        original_disable = manager._disable_lease

        async def blocked_disable(*args):
            disable_started.set()
            await allow_disable.wait()
            await original_disable(*args)

        manager._disable_lease = blocked_disable  # type: ignore[method-assign]
        invocation: asyncio.Task[object] | None = None
        try:
            tool = next(tool for tool in await manager.get_tools() if tool.name == "mcp_demo_slow")
            invocation = asyncio.create_task(
                tool.ainvoke(
                    {},
                    config={
                        "configurable": {
                            "insightagent_deadline_monotonic": time.monotonic() + 0.05,
                        }
                    },
                )
            )
            await asyncio.wait_for(started.wait(), timeout=0.5)
            await asyncio.wait_for(disable_started.wait(), timeout=0.5)
            invocation.cancel()
            await asyncio.sleep(0.01)
            assert not invocation.done()

            allow_disable.set()
            result = (await asyncio.gather(invocation, return_exceptions=True))[0]
            assert isinstance(result, asyncio.CancelledError)
            assert (await manager.status())["demo"]["state"] == "failed"
            unavailable = await tool.ainvoke({})
            assert unavailable["failure_kind"] == "mcp_cancellation_unconfirmed"
        finally:
            allow_disable.set()
            if invocation is not None:
                await asyncio.gather(invocation, return_exceptions=True)
            await manager.stop_all()

    asyncio.run(scenario())
