"""Slash command dispatcher for InsightAgent V5.0."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .agent import CodeAgent
from .context import ProjectMemory
from .session import SessionStore


class SlashCommandProcessor:
    def __init__(
        self,
        agent: CodeAgent,
        session_store: SessionStore | None = None,
        project_memory: ProjectMemory | None = None,
        mcp_manager: Any | None = None,
    ) -> None:
        self.agent = agent
        self.session_store = session_store
        self.project_memory = project_memory
        self.mcp_manager = mcp_manager

    def handle(self, command_line: str) -> str:
        parts = command_line.strip().split(maxsplit=1)
        command = parts[0] if parts else ""
        arg = parts[1] if len(parts) > 1 else ""
        if command == "/status":
            session_id = self.agent.session.session_id if self.agent.session else "none"
            phase = getattr(getattr(self.agent, "task_state", None), "phase", None)
            phase_value = phase.value if phase is not None else "unknown"
            return (
                f"session={session_id} phase={phase_value} messages={len(self.agent.messages)} "
                f"usage=({self.agent.usage_tracker.summary()})"
            )
        if command == "/cost":
            return self.agent.usage_tracker.summary()
        if command == "/memory":
            if self.project_memory is None or self.project_memory.is_empty:
                return "no project memory loaded"
            return self.project_memory.render()
        if command == "/compact":
            result = self.agent.compact_history()
            return f"compacted_messages={result['removed_message_count']}"
        if command == "/clear":
            self.agent.clear_history()
            return "conversation cleared"
        if command == "/permissions":
            context = getattr(self.agent.tools, "context", None)
            if context is None:
                return "permissions unavailable"
            return f"permission_mode={context.permission_mode} workspace={context.workspace}"
        if command == "/export":
            if self.session_store is None or self.agent.session is None:
                return "session export unavailable"
            path = Path(arg or f"{self.agent.session.session_id}.md")
            exported = self.session_store.export_markdown(self.agent.session, path)
            return f"exported={exported}"
        if command == "/mcp":
            return self._handle_mcp(arg)
        if command == "/help":
            return "/status /cost /memory /compact /clear /permissions /export [path] /mcp status|tools|restart|refresh /help"
        return f"unknown slash command: {command}"

    def _handle_mcp(self, arg: str) -> str:
        if self.mcp_manager is None:
            return "MCP unavailable"
        parts = arg.split()
        subcommand = parts[0] if parts else "status"
        if subcommand == "status":
            status = self.mcp_manager.status()
            if not status:
                return "MCP servers: none"
            lines = []
            for name, item in status.items():
                error = item.get("last_error") or ""
                suffix = f" error={error}" if error else ""
                lines.append(
                    (
                        f"{name}: state={item.get('state')} transport={item.get('transport')} "
                        f"tools={item.get('tools')} resources={item.get('resources')} prompts={item.get('prompts')}{suffix}"
                    )
                )
            return "\n".join(lines)
        if subcommand == "tools":
            tools = self.mcp_manager.get_tools()
            if not tools:
                return "MCP tools: none"
            return "\n".join(tool.name for tool in tools)
        if subcommand == "restart":
            if len(parts) < 2:
                return "usage: /mcp restart <server>"
            server = parts[1]
            if self.mcp_manager.restart_server(server):
                return f"MCP server restarted: {server}"
            return f"MCP server restart failed: {server}"
        if subcommand == "refresh":
            if len(parts) < 2:
                return "usage: /mcp refresh <server>"
            server = parts[1]
            if self.mcp_manager.refresh_server(server):
                return f"MCP server refreshed: {server}"
            return f"MCP server refresh failed: {server}"
        return f"unknown mcp command: {subcommand}"
