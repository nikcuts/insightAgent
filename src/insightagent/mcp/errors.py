"""MCP-specific errors."""

from __future__ import annotations


class MCPError(Exception):
    """Base class for MCP runtime errors."""


class MCPConfigError(MCPError):
    """Raised when MCP configuration is invalid."""


class MCPTransportError(MCPError):
    """Raised when MCP transport startup or I/O fails."""


class MCPProtocolError(MCPError):
    """Raised when JSON-RPC or MCP protocol handling fails."""


class MCPRequestTimeout(MCPTransportError):
    """Raised when an MCP request times out."""


class MCPToolError(MCPError):
    """Raised when an MCP tool call returns an error."""
