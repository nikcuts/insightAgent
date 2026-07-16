"""Async lifecycle management for official LangChain MCP tools."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, StructuredTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.sessions import Connection
from langchain_mcp_adapters.tools import load_mcp_tools
from pydantic import BaseModel, ConfigDict

from insightagent.graph.tools import remaining_seconds_from_config
from insightagent.runtime.tool_context import ToolContext
from insightagent.runtime.types import ToolSpec

from .adapters import (
    MCPToolRuntime,
    mcp_cancellation_unconfirmed_payload,
    mcp_success_payload,
    mcp_time_budget_payload,
)
from .config import MCPConfig, MCPServerConfig


ClientFactory = Callable[[dict[str, Connection]], Any]
ToolLoader = Callable[..., Awaitable[list[BaseTool]]]
TraceCallback = Callable[[dict[str, Any]], None]


@dataclass
class _ServerLease:
    name: str
    config: MCPServerConfig
    client: Any
    tools: list[BaseTool] = field(default_factory=list)
    specs: dict[str, ToolSpec] = field(default_factory=dict)
    tasks: set[asyncio.Task[object]] = field(default_factory=set)
    cancelling: set[asyncio.Task[object]] = field(default_factory=set)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    call_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    state: str = "running"
    last_error: str = ""
    resources: int = 0
    prompts: int = 0


class _NoArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _ResourceArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    uri: str


class _PromptArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str
    arguments: dict[str, object] = {}


class MCPManager:
    """Own MCP sessions and expose only LangChain tools to the graph runtime."""

    def __init__(
        self,
        config: MCPConfig,
        tool_context: ToolContext,
        *,
        client_factory: ClientFactory = MultiServerMCPClient,
        tool_loader: ToolLoader = load_mcp_tools,
    ) -> None:
        self.config = config
        self._tool_context = tool_context
        self._client_factory = client_factory
        self._tool_loader = tool_loader
        self.clients: dict[str, Any] = {}
        self.failed: dict[str, str] = {}
        self.conflicts: dict[str, str] = {}
        self._leases: dict[str, _ServerLease] = {}
        self._tools_cache: list[BaseTool] = []
        self._specs_cache: dict[str, ToolSpec] = {}
        self._generation = 0
        self._lifecycle_lock = asyncio.Lock()

    async def start_enabled(self, trace: TraceCallback | None = None) -> int:
        async with self._lifecycle_lock:
            return await self._start_enabled(trace)

    async def _start_enabled(self, trace: TraceCallback | None) -> int:
        started = 0
        for name, server_config in self.config.servers.items():
            if not server_config.enabled:
                continue
            _emit(trace, {"type": "mcp_server_starting", "server": name, "transport": server_config.transport})
            old = self._leases.get(name)
            if old is not None:
                await self._close_lease(old)
                self._leases.pop(name, None)
            self.clients.pop(name, None)
            self.failed.pop(name, None)
            try:
                lease = await _await_in_current_task(
                    self._open_server(name, server_config), server_config.startup_timeout
                )
            except Exception as error:
                self.failed[name] = _safe_error(error)
                _emit(trace, {"type": "mcp_server_failed", "server": name, "error": self.failed[name]})
                continue
            self._leases[name] = lease
            self.clients[name] = lease.client
            started += 1
            _emit(trace, {"type": "mcp_server_started", "server": name, "tools": len(lease.tools)})
        self._rebuild_caches()
        return started

    async def stop_all(self, trace: TraceCallback | None = None) -> None:
        _, cancellation_requested = await _complete_despite_cancellation(
            self._stop_all_with_lifecycle_lock(trace)
        )
        if cancellation_requested:
            raise asyncio.CancelledError

    async def _stop_all_with_lifecycle_lock(self, trace: TraceCallback | None) -> None:
        async with self._lifecycle_lock:
            await self._stop_all(trace)

    async def _stop_all(self, trace: TraceCallback | None) -> None:
        cancellation_requested = False
        for name, lease in list(self._leases.items()):
            _, cancelled = await _complete_despite_cancellation(
                self._close_lease_resources(lease)
            )
            cancellation_requested |= cancelled
            _emit(trace, {"type": "mcp_server_stopped", "server": name})
        self._leases.clear()
        self.clients.clear()
        self._tools_cache = []
        self._specs_cache = {}
        if cancellation_requested:
            raise asyncio.CancelledError

    async def restart_server(self, name: str, trace: TraceCallback | None = None) -> bool:
        async with self._lifecycle_lock:
            return await self._restart_server(name, trace)

    async def _restart_server(self, name: str, trace: TraceCallback | None) -> bool:
        server_config = self.config.servers.get(name)
        if server_config is None or not server_config.enabled:
            return False
        old = self._leases.get(name)
        if old is not None:
            await self._close_lease(old)
            self._leases.pop(name, None)
        self.clients.pop(name, None)
        self.failed.pop(name, None)
        try:
            lease = await _await_in_current_task(
                self._open_server(name, server_config), server_config.startup_timeout
            )
        except Exception as error:
            self.failed[name] = _safe_error(error)
            _emit(trace, {"type": "mcp_server_failed", "server": name, "error": self.failed[name]})
            self._rebuild_caches()
            return False
        self._leases[name] = lease
        self.clients[name] = lease.client
        self._generation += 1
        self._rebuild_caches()
        _emit(trace, {"type": "mcp_server_started", "server": name, "generation": self._generation})
        return True

    async def refresh_server(self, name: str, trace: TraceCallback | None = None) -> bool:
        return await self.restart_server(name, trace=trace)

    async def get_tools(self) -> list[BaseTool]:
        return list(self._tools_cache)

    async def get_tool_specs(self) -> dict[str, ToolSpec]:
        return dict(self._specs_cache)

    async def status(self) -> dict[str, dict[str, Any]]:
        status: dict[str, dict[str, Any]] = {}
        for name, server_config in self.config.servers.items():
            lease = self._leases.get(name)
            if lease is not None:
                item = {
                    "name": name,
                    "state": lease.state,
                    "transport": server_config.transport,
                    "tools": len(lease.tools),
                    "resources": lease.resources,
                    "prompts": lease.prompts,
                    "active_tasks": len(lease.tasks),
                    "last_error": lease.last_error,
                    "generation": self._generation,
                }
            elif name in self.failed:
                item = {
                    "name": name,
                    "state": "failed",
                    "transport": server_config.transport,
                    "tools": 0,
                    "resources": 0,
                    "prompts": 0,
                    "active_tasks": 0,
                    "last_error": self.failed[name],
                    "generation": self._generation,
                }
            else:
                item = {
                    "name": name,
                    "state": "disabled" if not server_config.enabled else "stopped",
                    "transport": server_config.transport,
                    "tools": 0,
                    "resources": 0,
                    "prompts": 0,
                    "active_tasks": 0,
                    "last_error": "",
                    "generation": self._generation,
                }
            if name in self.conflicts:
                item["last_error"] = self.conflicts[name]
            status[name] = item
        return status

    async def _open_server(self, name: str, server_config: MCPServerConfig) -> _ServerLease:
        connection = _connection_for(server_config)
        client = self._client_factory({name: connection})
        # Official adapters own a fresh session inside each tool invocation. This
        # keeps AnyIO session entry and exit in the same async task.
        source_tools = await self._tool_loader(
            None,
            connection=connection,
            server_name=name,
            tool_name_prefix=False,
            handle_tool_errors=False,
        )
        lease = _ServerLease(name, server_config, client)
        runtime = MCPToolRuntime(self._tool_context, name, lambda tool, spec, arguments, config: self._invoke(lease, tool, spec, arguments, config))
        prefix = server_config.tool_prefix or f"mcp_{name}"
        disabled = set(server_config.disabled_tools)
        for source_tool in source_tools:
            if source_tool.name in disabled:
                continue
            wrapped, spec = runtime.wrap(source_tool, f"{prefix}_{source_tool.name}")
            lease.tools.append(wrapped)
            lease.specs[spec.name] = spec
        await self._add_resource_and_prompt_tools(lease, runtime, prefix, disabled)
        return lease

    async def _add_resource_and_prompt_tools(
        self,
        lease: _ServerLease,
        runtime: MCPToolRuntime,
        prefix: str,
        disabled: set[str],
    ) -> None:
        resources = await self._discover_items(lease, "list_resources", "resources")
        prompts = await self._discover_items(lease, "list_prompts", "prompts")
        lease.resources = len(resources)
        lease.prompts = len(prompts)

        if resources:
            self._add_wrapped_tool(
                lease,
                runtime,
                prefix,
                "list_resources",
                "List resources exposed by this MCP server.",
                _NoArguments,
                self._list_resources_operation(lease),
                disabled,
            )
            self._add_wrapped_tool(
                lease,
                runtime,
                prefix,
                "read_resource",
                "Read an MCP resource by URI.",
                _ResourceArguments,
                self._read_resource_operation(lease),
                disabled,
            )
        if prompts:
            self._add_wrapped_tool(
                lease,
                runtime,
                prefix,
                "list_prompts",
                "List prompts exposed by this MCP server.",
                _NoArguments,
                self._list_prompts_operation(lease),
                disabled,
            )
            self._add_wrapped_tool(
                lease,
                runtime,
                prefix,
                "get_prompt",
                "Get an MCP prompt by name and arguments.",
                _PromptArguments,
                self._get_prompt_operation(lease),
                disabled,
            )

    async def _discover_items(
        self, lease: _ServerLease, method_name: str, item_name: str
    ) -> list[object]:
        try:
            async with lease.client.session(lease.name) as session:
                return await _list_paginated(session, method_name, item_name)
        except Exception:
            return []

    def _add_wrapped_tool(
        self,
        lease: _ServerLease,
        runtime: MCPToolRuntime,
        prefix: str,
        name: str,
        description: str,
        args_schema: type[BaseModel],
        operation: Callable[..., Awaitable[object]],
        disabled: set[str],
    ) -> None:
        if name in disabled:
            return
        source = StructuredTool(
            name=name,
            description=description,
            args_schema=args_schema,
            coroutine=operation,
            metadata={"readOnlyHint": True},
        )
        wrapped, spec = runtime.wrap(source, f"{prefix}_{name}")
        lease.tools.append(wrapped)
        lease.specs[spec.name] = spec

    def _list_resources_operation(self, lease: _ServerLease) -> Callable[..., Awaitable[object]]:
        async def operation(config: RunnableConfig) -> object:
            del config
            async with lease.client.session(lease.name) as session:
                return {"resources": await _list_paginated(session, "list_resources", "resources")}

        return operation

    def _read_resource_operation(self, lease: _ServerLease) -> Callable[..., Awaitable[object]]:
        async def operation(uri: str, config: RunnableConfig) -> object:
            del config
            async with lease.client.session(lease.name) as session:
                return await session.read_resource(uri)

        return operation

    def _list_prompts_operation(self, lease: _ServerLease) -> Callable[..., Awaitable[object]]:
        async def operation(config: RunnableConfig) -> object:
            del config
            async with lease.client.session(lease.name) as session:
                return {"prompts": await _list_paginated(session, "list_prompts", "prompts")}

        return operation

    def _get_prompt_operation(self, lease: _ServerLease) -> Callable[..., Awaitable[object]]:
        async def operation(
            name: str, arguments: dict[str, object], config: RunnableConfig
        ) -> object:
            del config
            async with lease.client.session(lease.name) as session:
                return await session.get_prompt(name, arguments)

        return operation

    async def _invoke(
        self,
        lease: _ServerLease,
        tool: BaseTool,
        spec: ToolSpec,
        arguments: dict[str, object],
        config: RunnableConfig,
    ) -> dict[str, object]:
        async with lease.call_lock:
            async with lease.lock:
                if lease.state != "running":
                    return mcp_cancellation_unconfirmed_payload(
                        spec, arguments, RuntimeError("MCP server is unavailable")
                    )
                remaining = remaining_seconds_from_config(config)
                if remaining is not None and remaining <= 0:
                    return mcp_time_budget_payload(spec, arguments)
                task = asyncio.create_task(tool.ainvoke(arguments, config=config))
                lease.tasks.add(task)
                task.add_done_callback(lambda _: _discard_finished_task(lease, task))
            try:
                if remaining is None:
                    result = await asyncio.shield(task)
                else:
                    result = await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
            except asyncio.TimeoutError:
                if task.done():
                    result = task.result()
                    return mcp_success_payload(spec, arguments, result)
                try:
                    cleanup_errors, cancellation_requested = await _wait_for_cancellation(lease, task)
                except Exception as error:
                    await self._disable_lease_after_cancellation(lease, error)
                    return mcp_cancellation_unconfirmed_payload(spec, arguments, error)
                error = cleanup_errors[0] if cleanup_errors else RuntimeError(
                    "MCP call was cancelled after the turn budget expired"
                )
                cancellation_requested |= await self._disable_lease_after_cancellation(lease, error)
                if cancellation_requested:
                    raise asyncio.CancelledError
                if cleanup_errors:
                    return mcp_cancellation_unconfirmed_payload(spec, arguments, error)
                return mcp_time_budget_payload(spec, arguments)
            except asyncio.CancelledError:
                cleanup_errors, _ = await _wait_for_cancellation(lease, task)
                error = cleanup_errors[0] if cleanup_errors else RuntimeError(
                    "MCP call was cancelled before it completed"
                )
                await self._disable_lease_after_cancellation(lease, error)
                raise
            finally:
                if task.done():
                    async with lease.lock:
                        lease.tasks.discard(task)
                        lease.cancelling.discard(task)
            return mcp_success_payload(spec, arguments, result)

    async def _disable_lease(self, lease: _ServerLease, error: BaseException) -> None:
        async with lease.lock:
            if lease.state == "running":
                lease.state = "failed"
                lease.last_error = _safe_error(error)
        if self._leases.get(lease.name) is lease:
            self.clients.pop(lease.name, None)
            self._rebuild_caches()

    async def _disable_lease_after_cancellation(
        self, lease: _ServerLease, error: BaseException
    ) -> bool:
        disable = asyncio.create_task(self._disable_lease(lease, error))
        cancellation_requested = False
        while True:
            try:
                await asyncio.shield(disable)
                return cancellation_requested
            except asyncio.CancelledError:
                cancellation_requested = True

    async def _close_lease(self, lease: _ServerLease) -> None:
        _, cancellation_requested = await _complete_despite_cancellation(
            self._close_lease_resources(lease)
        )
        if cancellation_requested:
            raise asyncio.CancelledError

    async def _close_lease_resources(self, lease: _ServerLease) -> None:
        async with lease.lock:
            if lease.state == "stopped":
                return
            lease.state = "stopping"
            tasks = list(lease.tasks)
            tasks_to_cancel = [task for task in tasks if task not in lease.cancelling]
            lease.cancelling.update(tasks_to_cancel)
        for task in tasks_to_cancel:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        async with lease.lock:
            lease.tasks.clear()
            lease.cancelling.clear()
            lease.state = "stopped"
        if self._leases.get(lease.name) is lease:
            self.clients.pop(lease.name, None)
            self._rebuild_caches()

    def _rebuild_caches(self) -> None:
        self.conflicts = {}
        tools: list[BaseTool] = []
        specs: dict[str, ToolSpec] = {}
        seen: set[str] = set()
        for name, lease in self._leases.items():
            if lease.state != "running":
                continue
            for tool in lease.tools:
                if tool.name in seen:
                    self.conflicts[name] = f"duplicate tool name: {tool.name}"
                    continue
                seen.add(tool.name)
                tools.append(tool)
                specs[tool.name] = lease.specs[tool.name]
        self._tools_cache = tools
        self._specs_cache = specs


def _connection_for(config: MCPServerConfig) -> Connection:
    if config.transport == "stdio":
        if not config.command:
            raise ValueError(f"stdio MCP server requires command: {config.name}")
        return {
            "transport": "stdio",
            "command": config.command,
            "args": config.args,
            "env": config.expanded_env(),
        }
    if config.transport == "streamable_http":
        if not config.url:
            raise ValueError(f"streamable_http MCP server requires url: {config.name}")
        return {
            "transport": "streamable_http",
            "url": config.url,
            "headers": config.expanded_headers(),
            "timeout": config.request_timeout,
        }
    raise ValueError(f"unsupported MCP transport: {config.transport}")


def _safe_error(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"


async def _list_paginated(session: Any, method_name: str, item_name: str) -> list[object]:
    method = getattr(session, method_name)
    result = await method()
    items = list(getattr(result, item_name, []) or [])
    cursor = _next_cursor(result)
    seen_cursors: set[str] = set()
    while cursor and cursor not in seen_cursors:
        seen_cursors.add(cursor)
        result = await method(cursor)
        items.extend(getattr(result, item_name, []) or [])
        cursor = _next_cursor(result)
    return items


def _next_cursor(result: object) -> str | None:
    cursor = getattr(result, "nextCursor", None)
    if cursor is None:
        cursor = getattr(result, "next_cursor", None)
    return cursor if isinstance(cursor, str) and cursor else None


def _discard_finished_task(lease: _ServerLease, task: asyncio.Task[object]) -> None:
    lease.tasks.discard(task)
    lease.cancelling.discard(task)


async def _wait_for_cancellation(
    lease: _ServerLease, task: asyncio.Task[object]
) -> tuple[list[BaseException], bool]:
    cleanup = asyncio.create_task(_cancel_and_collect(lease, task))
    cancellation_requested = False
    while True:
        try:
            return await asyncio.shield(cleanup), cancellation_requested
        except asyncio.CancelledError:
            cancellation_requested = True


async def _complete_despite_cancellation(awaitable: Awaitable[Any]) -> tuple[Any, bool]:
    task = asyncio.ensure_future(awaitable)
    cancellation_requested = False
    while True:
        try:
            return await asyncio.shield(task), cancellation_requested
        except asyncio.CancelledError:
            cancellation_requested = True


async def _cancel_and_collect(
    lease: _ServerLease, task: asyncio.Task[object]
) -> list[BaseException]:
    async with lease.lock:
        should_cancel = task not in lease.cancelling
        lease.cancelling.add(task)
    if should_cancel and not task.done():
        task.cancel()
    outcomes = await asyncio.gather(task, return_exceptions=True)
    return [
        outcome
        for outcome in outcomes
        if isinstance(outcome, BaseException) and not isinstance(outcome, asyncio.CancelledError)
    ]


async def _await_in_current_task(awaitable: Awaitable[Any], timeout: float) -> Any:
    """Apply a Python 3.10 timeout without moving AnyIO resources to another task."""
    if timeout <= 0:
        raise TimeoutError("MCP startup timeout must be positive")
    task = asyncio.current_task()
    if task is None:
        return await awaitable
    loop = asyncio.get_running_loop()
    expired = False

    def cancel_current_task() -> None:
        nonlocal expired
        expired = True
        task.cancel()

    timer = loop.call_later(timeout, cancel_current_task)
    try:
        return await awaitable
    except asyncio.CancelledError as error:
        if expired:
            raise TimeoutError("MCP startup timed out") from error
        raise
    finally:
        timer.cancel()


def _emit(trace: TraceCallback | None, event: dict[str, Any]) -> None:
    if trace is not None:
        trace(event)
