"""Official MCP-to-LangChain tool wrappers used by the async MCP manager."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, StructuredTool

from insightagent.runtime.failure_classifier import FailureClassifier
from insightagent.runtime.permissions import PermissionEnforcer
from insightagent.runtime.tool_context import ToolContext
from insightagent.runtime.types import ToolPermission, ToolRisk, ToolSpec


MCPToolCall = Callable[
    [BaseTool, ToolSpec, dict[str, object], RunnableConfig], Awaitable[dict[str, object]]
]


class MCPToolRuntime:
    """Applies the shared permission and failure policy to official MCP tools."""

    def __init__(
        self,
        context: ToolContext,
        server_name: str,
        call_tool: MCPToolCall,
        permission_enforcer: PermissionEnforcer | None = None,
        failure_classifier: FailureClassifier | None = None,
    ) -> None:
        self._context = context
        self._server_name = server_name
        self._call_tool = call_tool
        self._permission_enforcer = permission_enforcer or PermissionEnforcer()
        self._failure_classifier = failure_classifier or FailureClassifier()

    def wrap(self, tool: BaseTool, name: str) -> tuple[BaseTool, ToolSpec]:
        spec = _tool_spec(tool, name, self._server_name)
        args_schema = tool.args_schema
        if args_schema is None:
            args_schema = _tool_schema(tool)

        async def invoke(config: RunnableConfig, **arguments: object) -> dict[str, object]:
            decision = self._permission_enforcer.check(spec, self._context, arguments)
            if not decision.allowed:
                return _error_payload(
                    spec,
                    arguments,
                    content=f"PermissionDenied: {decision.reason}",
                    failure_kind="permission_denied",
                    retryable=False,
                )
            try:
                return await self._call_tool(tool, spec, arguments, config)
            except Exception as error:
                classification = self._failure_classifier.classify_exception(error, spec.name)
                return _error_payload(
                    spec,
                    arguments,
                    content=f"{type(error).__name__}: {error}",
                    failure_kind=classification.kind.value,
                    retryable=classification.retryable,
                    repair_guidance=classification.repair_guidance,
                )

        return (
            StructuredTool(
                name=name,
                description=tool.description,
                args_schema=args_schema,
                coroutine=invoke,
                metadata={**dict(tool.metadata or {}), "mcp_server": self._server_name},
            ),
            spec,
        )


def mcp_success_payload(
    spec: ToolSpec, arguments: Mapping[str, object], content: object
) -> dict[str, object]:
    return {
        "name": spec.name,
        "arguments": dict(arguments),
        "content": _json_content(content),
        "is_error": False,
        "failure_kind": None,
        "retryable": False,
        "permission": spec.required_permission.value,
        "risk": spec.risk.value,
        "mcp_server": spec.mcp_server,
        "metadata": {"mcp_server": spec.mcp_server},
    }


def mcp_time_budget_payload(
    spec: ToolSpec, arguments: Mapping[str, object]
) -> dict[str, object]:
    return _error_payload(
        spec,
        arguments,
        content="time_budget_exceeded: MCP call exceeded the remaining turn budget",
        failure_kind="time_budget_exceeded",
        retryable=False,
        repair_guidance="The remaining turn budget expired. Narrow the next action or report the timeout.",
    )


def mcp_cancellation_unconfirmed_payload(
    spec: ToolSpec, arguments: Mapping[str, object], error: BaseException
) -> dict[str, object]:
    return _error_payload(
        spec,
        arguments,
        content=f"mcp_cancellation_unconfirmed: {type(error).__name__}: {error}",
        failure_kind="mcp_cancellation_unconfirmed",
        retryable=False,
        repair_guidance="The MCP session could not be closed safely. Do not retry this server in the current turn.",
    )


def _tool_spec(tool: BaseTool, name: str, server_name: str) -> ToolSpec:
    metadata = dict(tool.metadata or {})
    read_only = bool(metadata.get("readOnlyHint") or metadata.get("read_only_hint"))
    if read_only:
        permission = ToolPermission.READ
        risk = ToolRisk.LOW
        mutates_workspace = False
        uses_network = False
    else:
        permission = ToolPermission.MCP
        risk = ToolRisk.HIGH
        mutates_workspace = True
        uses_network = True
    return ToolSpec(
        name=name,
        description=tool.description,
        input_schema=_tool_schema(tool),
        required_permission=permission,
        risk=risk,
        mutates_workspace=mutates_workspace,
        uses_network=uses_network,
        mcp_server=server_name,
        tags=("mcp", server_name),
    )


def _tool_schema(tool: BaseTool) -> dict[str, object]:
    schema = tool.tool_call_schema
    if isinstance(schema, dict):
        return dict(schema)
    model_json_schema = getattr(schema, "model_json_schema", None)
    if callable(model_json_schema):
        value = model_json_schema()
        if isinstance(value, dict):
            return value
    return {"type": "object", "properties": {}}


def _error_payload(
    spec: ToolSpec,
    arguments: Mapping[str, object],
    *,
    content: str,
    failure_kind: str,
    retryable: bool,
    repair_guidance: str = "",
) -> dict[str, object]:
    return {
        "name": spec.name,
        "arguments": dict(arguments),
        "content": content,
        "is_error": True,
        "failure_kind": failure_kind,
        "retryable": retryable,
        "repair_guidance": repair_guidance,
        "permission": spec.required_permission.value,
        "risk": spec.risk.value,
        "mcp_server": spec.mcp_server,
        "metadata": {"mcp_server": spec.mcp_server},
    }


def _json_content(value: object) -> object:
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_content(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_content(item) for item in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _json_content(model_dump(mode="json"))
    return json.dumps(value, ensure_ascii=False, default=str)
