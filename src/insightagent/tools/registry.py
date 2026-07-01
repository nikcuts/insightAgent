"""Tool registry and default tool construction."""

from __future__ import annotations

import json
from collections import OrderedDict
from pathlib import Path
from typing import Any

from ..api.resilience import BACKOFF_FAILURE_KINDS, PERMANENT_FAILURE_KINDS, RetryPolicy
from ..runtime.failure_classifier import FailureClassifier, FailureKind
from ..runtime.permissions import PermissionEnforcer
from ..runtime.types import ToolExecutionResult, ToolPermission, ToolRisk, ToolSpec
from ..runtime.tool_context import ToolContext


DUPLICATE_CALL_MESSAGE = (
    "这个调用刚刚执行过，结果没有变化，请基于上面已有的结果继续推进，不要再重复调用相同的读取/查询。"
)
_MAX_RECENT_SUCCESS = 16
from .base import Tool
from .code_analysis_tools import (
    FindDependenciesTool,
    GetCodeMetricsTool,
    GetFunctionSignatureTool,
    ParseAstTool,
)
from .execution_tools import ExecuteCommandTool, RunVerificationTool
from .file_tools import EditFileTool, ReadFileTool, WriteFileTool
from .search_tools import GrepSearchTool, GlobSearchTool
from .state_tools import GitDiffTool, GitStatusTool, LspDiagnosticsTool, TodoWriteTool


class ToolRegistry:
    def __init__(
        self,
        tools: list[Tool] | None = None,
        context: ToolContext | None = None,
        retry_policy: RetryPolicy | None = None,
        resilience_enabled: bool = True,
    ) -> None:
        self.context = context or ToolContext(workspace=Path.cwd())
        resolved_tools = default_tools(self.context) if tools is None else tools
        self._tools: dict[str, Tool] = {}
        self._specs: dict[str, ToolSpec] = {}
        self._failure_classifier = FailureClassifier()
        self._permission_enforcer = PermissionEnforcer()
        self._retry_policy = retry_policy or RetryPolicy()
        self._resilience_enabled = resilience_enabled
        self._non_retryable_failures: dict[str, ToolExecutionResult] = {}
        self._recent_success: "OrderedDict[str, bool]" = OrderedDict()
        self._backoff_attempts: dict[str, int] = {}
        for tool in resolved_tools:
            if tool.name in self._tools:
                raise ValueError(f"duplicate tool name: {tool.name}")
            self._tools[tool.name] = tool
            self._specs[tool.name] = _spec_for_tool(tool)

    def schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema,
            }
            for tool in self._tools.values()
        ]

    def run(self, name: str, arguments: dict[str, Any]) -> str:
        if name not in self._tools:
            raise KeyError(f"unknown tool: {name}")
        return self._tools[name].run(arguments)

    def spec(self, name: str) -> ToolSpec:
        if name not in self._specs:
            raise KeyError(f"unknown tool: {name}")
        return self._specs[name]

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolExecutionResult:
        if name not in self._tools:
            classification = self._failure_classifier.classify(name, f"unknown tool: {name}", is_error=True)
            return ToolExecutionResult(
                name=name,
                arguments=arguments,
                content=f"KeyError: unknown tool: {name}",
                is_error=True,
                failure_kind=classification.kind,
                retryable=classification.retryable,
                repair_guidance=classification.repair_guidance,
            )
        spec = self._specs[name]
        signature = _tool_signature(name, arguments)
        if self._resilience_enabled and self._is_read_only(spec) and signature in self._recent_success:
            self._recent_success.move_to_end(signature)
            return ToolExecutionResult(
                name=name,
                arguments=arguments,
                content=DUPLICATE_CALL_MESSAGE,
                is_error=False,
                failure_kind=None,
                retryable=False,
                repair_guidance="",
                permission=spec.required_permission,
                risk=spec.risk,
                suppressed=True,
                metadata={"signature": signature, "duplicate": True},
            )
        prior_failure = self._non_retryable_failures.get(signature)
        if prior_failure is not None:
            repeat_count = prior_failure.repeat_count + 1
            return ToolExecutionResult(
                name=name,
                arguments=arguments,
                content=(
                    "Repeated non-retryable tool call suppressed by runtime harness. "
                    f"failure_kind={_failure_value(prior_failure.failure_kind)}; "
                    f"prior_result:\n{prior_failure.content}"
                ),
                is_error=True,
                failure_kind=prior_failure.failure_kind,
                retryable=False,
                repair_guidance=prior_failure.repair_guidance,
                permission=spec.required_permission,
                risk=spec.risk,
                command_kind=prior_failure.command_kind,
                suppressed=True,
                repeat_count=repeat_count,
                metadata={"signature": signature},
            )
        permission = self._permission_enforcer.check(spec, self.context, arguments)
        if not permission.allowed:
            content = f"PermissionDenied: {permission.reason}"
            classification = self._failure_classifier.classify(name, content, is_error=True)
            return ToolExecutionResult(
                name=name,
                arguments=arguments,
                content=content,
                is_error=True,
                failure_kind=FailureKind.PERMISSION_DENIED,
                retryable=False,
                repair_guidance=classification.repair_guidance,
                permission=spec.required_permission,
                risk=spec.risk,
                command_kind=permission.command_kind,
                metadata={"signature": signature},
            )
        backoff_delay = 0.0
        prior_attempts = self._backoff_attempts.get(signature, 0)
        if self._resilience_enabled and prior_attempts > 0:
            backoff_delay = self._retry_policy.wait(prior_attempts)
        metadata: dict[str, Any] = {"signature": signature}
        if backoff_delay > 0:
            metadata["backoff_delay"] = backoff_delay
            metadata["backoff_attempt"] = prior_attempts
        try:
            content = self._tools[name].run(arguments)
            is_error = _looks_like_error(name, content)
            classification = self._failure_classifier.classify(name, content, is_error=is_error)
            result = ToolExecutionResult(
                name=name,
                arguments=arguments,
                content=content,
                is_error=is_error,
                failure_kind=classification.kind if is_error else None,
                retryable=classification.retryable,
                repair_guidance=classification.repair_guidance,
                permission=spec.required_permission,
                risk=spec.risk,
                command_kind=permission.command_kind,
                metadata=metadata,
            )
        except Exception as error:
            classification = self._failure_classifier.classify(name, f"{type(error).__name__}: {error}", True, error)
            result = ToolExecutionResult(
                name=name,
                arguments=arguments,
                content=f"{type(error).__name__}: {error}",
                is_error=True,
                failure_kind=classification.kind,
                retryable=classification.retryable,
                repair_guidance=classification.repair_guidance,
                permission=spec.required_permission,
                risk=spec.risk,
                command_kind=permission.command_kind,
                metadata=metadata,
            )
        self._record_outcome(signature, spec, result)
        return result

    def _record_outcome(self, signature: str, spec: ToolSpec, result: ToolExecutionResult) -> None:
        if self._resilience_enabled and not self._is_read_only(spec):
            self._recent_success.clear()
        if not result.is_error:
            self._backoff_attempts.pop(signature, None)
            if self._resilience_enabled and self._is_read_only(spec):
                self._recent_success[signature] = True
                self._recent_success.move_to_end(signature)
                while len(self._recent_success) > _MAX_RECENT_SUCCESS:
                    self._recent_success.popitem(last=False)
            return
        kind = result.failure_kind
        if not self._resilience_enabled:
            # Legacy behaviour: suppress repeats of these failures immediately
            # (no exponential-backoff retry).
            if kind in BACKOFF_FAILURE_KINDS or kind in PERMANENT_FAILURE_KINDS:
                self._non_retryable_failures[signature] = result
            return
        if kind in BACKOFF_FAILURE_KINDS:
            attempt = self._backoff_attempts.get(signature, 0) + 1
            self._backoff_attempts[signature] = attempt
            if attempt >= self._retry_policy.max_attempts:
                self._non_retryable_failures[signature] = result
        elif kind in PERMANENT_FAILURE_KINDS:
            self._non_retryable_failures[signature] = result

    @staticmethod
    def _is_read_only(spec: ToolSpec) -> bool:
        return spec.required_permission == ToolPermission.READ


def default_tools(context: ToolContext | None = None) -> list[Tool]:
    resolved_context = context or ToolContext(workspace=Path.cwd())
    return [
        ExecuteCommandTool(resolved_context),
        RunVerificationTool(resolved_context),
        ReadFileTool(resolved_context),
        WriteFileTool(resolved_context),
        EditFileTool(resolved_context),
        GrepSearchTool(resolved_context),
        GlobSearchTool(resolved_context),
        GitStatusTool(resolved_context),
        GitDiffTool(resolved_context),
        TodoWriteTool(resolved_context),
        LspDiagnosticsTool(resolved_context),
        ParseAstTool(resolved_context),
        GetFunctionSignatureTool(resolved_context),
        FindDependenciesTool(resolved_context),
        GetCodeMetricsTool(resolved_context),
    ]


def _spec_for_tool(tool: Tool) -> ToolSpec:
    name = tool.name
    permission = ToolPermission.READ
    risk = ToolRisk.LOW
    mutates = False
    executes = False
    uses_network = False
    tags: tuple[str, ...] = ()
    if name in {"write_file", "edit_file", "todo_write"}:
        permission = ToolPermission.WORKSPACE_WRITE
        risk = ToolRisk.MEDIUM
        mutates = True
        tags = ("workspace", "write")
    elif name == "execute_command":
        permission = ToolPermission.EXECUTE
        risk = ToolRisk.HIGH
        executes = True
        tags = ("shell", "command")
    elif name == "run_verification":
        permission = ToolPermission.EXECUTE
        risk = ToolRisk.HIGH
        executes = True
        tags = ("shell", "verify")
    elif name.startswith("mcp_"):
        permission = ToolPermission.MCP
        risk = ToolRisk.MEDIUM
        uses_network = True
        tags = ("mcp", "external")
    elif name in {"read_file", "grep_search", "glob_search", "git_status", "git_diff"}:
        tags = ("workspace", "read")
    elif name in {"parse_ast", "get_function_signature", "find_dependencies", "get_code_metrics", "lsp_diagnostics"}:
        tags = ("analysis", "read")
    return ToolSpec(
        name=name,
        description=tool.description,
        input_schema=tool.input_schema,
        required_permission=permission,
        risk=risk,
        mutates_workspace=mutates,
        executes_code=executes,
        uses_network=uses_network,
        tags=tags,
    )


def _tool_signature(name: str, arguments: dict[str, Any]) -> str:
    return name + ":" + json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str)


def _looks_like_error(name: str, content: str) -> bool:
    if name in {"execute_command", "run_verification"}:
        return not content.startswith("exit_code: 0\n")
    return False


def _failure_value(kind: Any | None) -> str:
    if kind is None:
        return ""
    return getattr(kind, "value", str(kind))
