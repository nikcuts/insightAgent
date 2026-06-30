"""Manager for multiple MCP clients."""

from __future__ import annotations

from typing import Any, Callable

from .adapters import tools_for_client
from .client import MCPClient
from .config import MCPConfig, MCPServerConfig


ClientFactory = Callable[[MCPServerConfig], Any]


class MCPManager:
    def __init__(self, config: MCPConfig, client_factory: ClientFactory = MCPClient) -> None:
        self.config = config
        self.client_factory = client_factory
        self.clients: dict[str, Any] = {}
        self.failed: dict[str, str] = {}
        self.conflicts: dict[str, str] = {}
        self._tools_cache: list[Any] = []

    def start_enabled(self, trace: Callable[[dict[str, Any]], None] | None = None) -> int:
        started = 0
        for name, server_config in self.config.servers.items():
            if not server_config.enabled:
                continue
            _emit(trace, {"type": "mcp_server_starting", "server": name, "transport": server_config.transport})
            client = self.client_factory(server_config)
            try:
                client.start()
            except Exception as error:
                self.failed[name] = str(error)
                _emit(trace, {"type": "mcp_server_failed", "server": name, "error": str(error)})
                continue
            self.clients[name] = client
            started += 1
            _emit(
                trace,
                {
                    "type": "mcp_server_started",
                    "server": name,
                    "tools": len(getattr(client, "tools", [])),
                    "resources": len(getattr(client, "resources", [])),
                    "prompts": len(getattr(client, "prompts", [])),
                },
            )
        self._rebuild_tools_cache()
        return started

    def stop_all(self, trace: Callable[[dict[str, Any]], None] | None = None) -> None:
        for name, client in list(self.clients.items()):
            client.stop()
            _emit(trace, {"type": "mcp_server_stopped", "server": name})
        self.clients.clear()
        self._tools_cache = []

    def restart_server(self, name: str, trace: Callable[[dict[str, Any]], None] | None = None) -> bool:
        if name not in self.config.servers or not self.config.servers[name].enabled:
            return False
        old_client = self.clients.pop(name, None)
        if old_client is not None:
            old_client.stop()
        self.failed.pop(name, None)
        client = self.client_factory(self.config.servers[name])
        try:
            client.start()
        except Exception as error:
            self.failed[name] = str(error)
            _emit(trace, {"type": "mcp_server_failed", "server": name, "error": str(error)})
            self._rebuild_tools_cache()
            return False
        self.clients[name] = client
        _emit(trace, {"type": "mcp_server_started", "server": name})
        self._rebuild_tools_cache()
        return True

    def refresh_server(self, name: str) -> bool:
        client = self.clients.get(name)
        if client is None:
            return False
        client.refresh_capabilities()
        self._rebuild_tools_cache()
        return True

    def get_tools(self) -> list[Any]:
        return list(self._tools_cache)

    def status(self) -> dict[str, dict[str, Any]]:
        status: dict[str, dict[str, Any]] = {}
        for name, config in self.config.servers.items():
            client = self.clients.get(name)
            if client is not None:
                server_status = dict(client.status())
            elif name in self.failed:
                server_status = {
                    "name": name,
                    "state": "failed",
                    "transport": config.transport,
                    "tools": 0,
                    "resources": 0,
                    "prompts": 0,
                    "last_error": self.failed[name],
                    "stderr": "",
                }
            else:
                server_status = {
                    "name": name,
                    "state": "disabled" if not config.enabled else "stopped",
                    "transport": config.transport,
                    "tools": 0,
                    "resources": 0,
                    "prompts": 0,
                    "last_error": "",
                    "stderr": "",
                }
            if name in self.conflicts:
                server_status["last_error"] = self.conflicts[name]
            status[name] = server_status
        return status

    def _rebuild_tools_cache(self) -> None:
        self.conflicts = {}
        tools: list[Any] = []
        seen: set[str] = set()
        for name, client in self.clients.items():
            config = self.config.servers[name]
            prefix = config.tool_prefix or f"mcp_{name}"
            for tool in tools_for_client(client, prefix, config.disabled_tools):
                if tool.name in seen:
                    self.conflicts[name] = f"duplicate tool name: {tool.name}"
                    continue
                seen.add(tool.name)
                tools.append(tool)
        self._tools_cache = tools


def _emit(trace: Callable[[dict[str, Any]], None] | None, event: dict[str, Any]) -> None:
    if trace is not None:
        trace(event)
