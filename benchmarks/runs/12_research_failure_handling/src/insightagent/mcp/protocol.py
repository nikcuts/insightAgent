"""JSON-RPC helpers for MCP."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .errors import MCPProtocolError


SUPPORTED_PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = {SUPPORTED_PROTOCOL_VERSION}


@dataclass
class JsonRpcIdGenerator:
    current: int = 0

    def next(self) -> int:
        self.current += 1
        return self.current


def build_request(message_id: int, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": message_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def build_notification(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        message["params"] = params
    return message


def parse_response(message: dict[str, Any], expected_id: int) -> Any:
    if message.get("jsonrpc") != "2.0":
        raise MCPProtocolError(f"invalid JSON-RPC version: {message.get('jsonrpc')}")
    if message.get("id") != expected_id:
        raise MCPProtocolError(f"unexpected response id: {message.get('id')} expected={expected_id}")
    if "error" in message:
        error = message["error"]
        if isinstance(error, dict):
            raise MCPProtocolError(f"MCP error {error.get('code')}: {error.get('message')}")
        raise MCPProtocolError(f"MCP error: {error}")
    if "result" not in message:
        raise MCPProtocolError("JSON-RPC response missing result")
    return message["result"]
