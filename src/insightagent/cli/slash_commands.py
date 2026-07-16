"""Async slash commands backed exclusively by the graph runtime."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any


class GraphSlashCommandProcessor:
    """Read and control a long-lived ``GraphRunner`` without legacy sessions."""

    def __init__(self, runner: Any, *, thread_id: str, workspace: Path) -> None:
        self.runner = runner
        self.thread_id = thread_id
        self.workspace = workspace.expanduser().resolve()

    async def handle(self, command_line: str) -> str:
        parts = command_line.strip().split(maxsplit=1)
        command = parts[0] if parts else ""
        argument = parts[1] if len(parts) > 1 else ""
        if command == "/status":
            return await self._status()
        if command == "/cost":
            return await self._cost()
        if command == "/memory":
            return self._memory()
        if command == "/compact":
            result = await self.runner.compact_thread(self.thread_id)
            return (
                f"removed_messages={result.get('removed_messages', 0)} "
                f"summarized_tool_events={result.get('summarized_tool_events', 0)}"
            )
        if command == "/clear":
            previous_thread_id = self.thread_id
            self.thread_id = uuid.uuid4().hex
            return f"thread_id={self.thread_id} cleared_previous={previous_thread_id}"
        if command == "/permissions":
            return (
                f"permission_mode={self.runner.permission_mode} "
                f"tool_profile={self.runner.tool_profile} workspace={self.workspace}"
            )
        if command == "/export":
            destination = Path(argument) if argument else self.workspace / f"{self.thread_id}.md"
            exported = await self.runner.export_transcript(self.thread_id, destination)
            return f"exported={exported}"
        if command == "/mcp":
            return await self._mcp(argument)
        if command == "/help":
            return "/status /cost /memory /compact /clear /permissions /export [path] /mcp status|tools|restart|refresh /help"
        return f"unknown slash command: {command}"

    async def _status(self) -> str:
        snapshot = await self.runner.latest_state(self.thread_id)
        values = _mapping(getattr(snapshot, "values", None))
        config = _mapping(getattr(snapshot, "config", None))
        configurable = _mapping(config.get("configurable"))
        return (
            f"thread_id={self.thread_id} phase={values.get('phase', 'unknown')} "
            f"checkpoint_id={configurable.get('checkpoint_id', 'unknown')} "
            f"workspace={values.get('workspace', self.workspace)} "
            f"iteration={values.get('iteration', 0)}"
        )

    async def _cost(self) -> str:
        snapshot = await self.runner.latest_state(self.thread_id)
        values = _mapping(getattr(snapshot, "values", None))
        usage = _mapping(values.get("usage"))
        trace_id = self.runner.last_trace_id(self.thread_id)
        return (
            f"input_tokens={_token(usage.get('input_tokens'))} "
            f"output_tokens={_token(usage.get('output_tokens'))} "
            f"total_tokens={_token(usage.get('total_tokens'))} "
            f"trace_id={trace_id or 'unavailable'}"
        )

    def _memory(self) -> str:
        filenames = tuple(self.runner.project_memory_filenames)
        return "memory_files=" + (", ".join(filenames) if filenames else "none")

    async def _mcp(self, argument: str) -> str:
        parts = argument.split()
        subcommand = parts[0] if parts else "status"
        if subcommand == "status":
            status = await self.runner.mcp_status()
            if not status:
                return "MCP servers: none"
            return "\n".join(
                f"{name}: state={item.get('state', 'unknown')} tools={item.get('tools', 0)}"
                + (f" error={item['last_error']}" if item.get("last_error") else "")
                for name, item in status.items()
            )
        if subcommand == "tools":
            tools = await self.runner.mcp_tool_names()
            return "\n".join(tools) if tools else "MCP tools: none"
        if subcommand in {"restart", "refresh"}:
            if len(parts) < 2:
                return f"usage: /mcp {subcommand} <server>"
            server = parts[1]
            succeeded = (
                await self.runner.restart_mcp(server)
                if subcommand == "restart"
                else await self.runner.refresh_mcp(server)
            )
            return (
                f"MCP server {subcommand}ed: {server}"
                if succeeded
                else f"MCP server {subcommand} failed: {server}"
            )
        return f"unknown mcp command: {subcommand}"


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _token(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


__all__ = ["GraphSlashCommandProcessor"]
