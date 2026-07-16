"""MCP configuration errors."""

from __future__ import annotations

from collections.abc import Iterable


class MCPError(Exception):
    """Base class for MCP runtime errors."""


class MCPConfigError(MCPError):
    """Raised when MCP configuration is invalid."""


class MCPStartupError(MCPConfigError):
    """Raised when a server selected for this run cannot be started."""

    def __init__(self, server_names: Iterable[str]) -> None:
        names = sorted({name for name in server_names if name})
        super().__init__(f"MCP server startup failed: {', '.join(names) or 'unknown'}")
