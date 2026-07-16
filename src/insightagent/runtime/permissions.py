"""Permission enforcement for tool execution."""

from __future__ import annotations

from typing import Any

from .tool_context import ToolContext
from .command_validation import CommandValidator
from .types import PermissionDecision, ToolPermission, ToolSpec


class PermissionEnforcer:
    def __init__(self, command_validator: CommandValidator | None = None) -> None:
        self.command_validator = command_validator or CommandValidator()

    def check(self, spec: ToolSpec, context: ToolContext, arguments: dict[str, Any]) -> PermissionDecision:
        if spec.required_permission == ToolPermission.WORKSPACE_WRITE and not context.can_write:
            return PermissionDecision(
                allowed=False,
                required_permission=spec.required_permission,
                reason=f"{spec.name} requires workspace-write permission but runtime is read-only",
            )
        if spec.required_permission == ToolPermission.MCP and not context.can_write:
            return PermissionDecision(
                allowed=False,
                required_permission=spec.required_permission,
                reason=f"{spec.name} is not declared read-only and runtime is read-only",
            )
        if spec.required_permission == ToolPermission.EXECUTE:
            command = str(arguments.get("command", ""))
            decision = self.command_validator.validate(command, context.permission_mode)
            if not decision.allowed:
                return PermissionDecision(
                    allowed=False,
                    required_permission=spec.required_permission,
                    reason=decision.reason,
                    command_kind=decision.kind.value,
                )
            return PermissionDecision(
                allowed=True,
                required_permission=spec.required_permission,
                command_kind=decision.kind.value,
            )
        return PermissionDecision(allowed=True, required_permission=spec.required_permission)
