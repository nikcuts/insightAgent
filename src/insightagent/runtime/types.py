"""Shared runtime harness types."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ToolPermission(str, Enum):
    NONE = "none"
    READ = "read"
    WORKSPACE_WRITE = "workspace-write"
    EXECUTE = "execute"
    MCP = "mcp"
    EXTERNAL = "external"


class ToolRisk(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    required_permission: ToolPermission = ToolPermission.READ
    risk: ToolRisk = ToolRisk.LOW
    mutates_workspace: bool = False
    executes_code: bool = False
    uses_network: bool = False
    mcp_server: str | None = None
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class PermissionDecision:
    allowed: bool
    required_permission: ToolPermission
    reason: str = ""
    command_kind: str | None = None


@dataclass(frozen=True)
class ToolExecutionResult:
    name: str
    arguments: dict[str, Any]
    content: str
    is_error: bool
    failure_kind: Any | None = None
    retryable: bool = False
    repair_guidance: str = ""
    permission: ToolPermission = ToolPermission.READ
    risk: ToolRisk = ToolRisk.LOW
    command_kind: str | None = None
    suppressed: bool = False
    repeat_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
