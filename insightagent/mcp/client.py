"""MCP client for a single server."""

from __future__ import annotations

from typing import Any

from .config import MCPServerConfig
from .errors import MCPProtocolError, MCPTransportError
from .protocol import SUPPORTED_PROTOCOL_VERSION, SUPPORTED_PROTOCOL_VERSIONS
from .transports import StdioTransport, StreamableHttpTransport


class MCPClient:
    def __init__(self, server_config: MCPServerConfig, transport: Any | None = None) -> None:
        self.server_config = server_config
        self.name = server_config.name
        self.transport = transport or self._build_transport(server_config)
        self.state = "created"
        self.protocol_version: str | None = None
        self.server_capabilities: dict[str, Any] = {}
        self.server_info: dict[str, Any] = {}
        self.instructions: str | None = None
        self.tools: list[dict[str, Any]] = []
        self.resources: list[dict[str, Any]] = []
        self.prompts: list[dict[str, Any]] = []
        self.last_error = ""

    def start(self) -> None:
        self.state = "starting"
        try:
            self.transport.start()
            result = self.transport.send_request(
                "initialize",
                {
                    "protocolVersion": SUPPORTED_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "insightagent", "version": "5.0"},
                },
                timeout=self.server_config.startup_timeout,
            )
            protocol_version = result.get("protocolVersion")
            if protocol_version not in SUPPORTED_PROTOCOL_VERSIONS:
                raise MCPProtocolError(f"unsupported MCP protocol version: {protocol_version}")
            self.protocol_version = protocol_version
            set_version = getattr(self.transport, "set_protocol_version", None)
            if callable(set_version):
                set_version(protocol_version)
            self.server_capabilities = _object_or_empty(result.get("capabilities"))
            self.server_info = _object_or_empty(result.get("serverInfo"))
            instructions = result.get("instructions")
            self.instructions = instructions if isinstance(instructions, str) else None
            self.transport.send_notification("notifications/initialized")
            self.state = "initialized"
            self.refresh_capabilities()
            self.state = "running"
        except Exception as error:
            self.last_error = str(error)
            self.state = "failed"
            self.transport.stop()
            raise

    def stop(self) -> None:
        self.transport.stop()
        self.state = "stopped"

    def refresh_capabilities(self) -> None:
        self.tools = _list_from_result(self._request_or_empty("tools/list"), "tools")
        self.resources = _list_from_result(self._request_or_empty("resources/list"), "resources")
        self.prompts = _list_from_result(self._request_or_empty("prompts/list"), "prompts")

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.transport.send_request(
            "tools/call",
            {"name": name, "arguments": arguments},
            timeout=self.server_config.request_timeout,
        )

    def list_resources(self) -> dict[str, Any]:
        return self.transport.send_request("resources/list", timeout=self.server_config.request_timeout)

    def read_resource(self, uri: str) -> dict[str, Any]:
        return self.transport.send_request(
            "resources/read",
            {"uri": uri},
            timeout=self.server_config.request_timeout,
        )

    def list_prompts(self) -> dict[str, Any]:
        return self.transport.send_request("prompts/list", timeout=self.server_config.request_timeout)

    def get_prompt(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.transport.send_request(
            "prompts/get",
            {"name": name, "arguments": arguments or {}},
            timeout=self.server_config.request_timeout,
        )

    def status(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state,
            "transport": self.server_config.transport,
            "protocol_version": self.protocol_version,
            "tools": len(self.tools),
            "resources": len(self.resources),
            "prompts": len(self.prompts),
            "last_error": self.last_error,
            "stderr": self.transport.stderr_summary(),
        }

    def _request_or_empty(self, method: str) -> dict[str, Any]:
        try:
            return self.transport.send_request(method, timeout=self.server_config.request_timeout)
        except MCPProtocolError as error:
            if "method not found" in str(error) or "-32601" in str(error):
                return {}
            raise

    def _build_transport(self, config: MCPServerConfig) -> Any:
        if config.transport == "stdio":
            if not config.command:
                raise MCPTransportError(f"stdio MCP server requires command: {config.name}")
            return StdioTransport(config.name, config.command, config.args, config.expanded_env())
        if config.transport == "streamable_http":
            if not config.url:
                raise MCPTransportError(f"streamable_http MCP server requires url: {config.name}")
            return StreamableHttpTransport(
                config.name,
                config.url,
                headers=config.expanded_headers(),
                request_timeout=config.request_timeout,
            )
        raise MCPTransportError(f"unsupported MCP transport: {config.transport}")


def _object_or_empty(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list_from_result(result: dict[str, Any], key: str) -> list[dict[str, Any]]:
    values = result.get(key, [])
    if not isinstance(values, list):
        return []
    return [value for value in values if isinstance(value, dict)]
