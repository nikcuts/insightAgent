"""Adapters from MCP capabilities to InsightAgent tools."""

from __future__ import annotations

import json
from typing import Any


def format_mcp_result(result: dict[str, Any]) -> str:
    parts: list[str] = []
    if result.get("isError"):
        parts.append("MCP tool error:")
    for item in result.get("content") or result.get("contents") or []:
        if not isinstance(item, dict):
            parts.append(str(item))
            continue
        item_type = item.get("type")
        if item_type == "text" and isinstance(item.get("text"), str):
            parts.append(item["text"])
        elif item_type == "resource":
            parts.append(_format_resource(item.get("resource", item)))
        elif "uri" in item:
            parts.append(_format_resource(item))
        elif isinstance(item.get("text"), str):
            parts.append(item["text"])
        else:
            parts.append(json.dumps(item, ensure_ascii=False, sort_keys=True))
    if "structuredContent" in result:
        parts.append(json.dumps(result["structuredContent"], ensure_ascii=False, sort_keys=True))
    if "resources" in result:
        parts.append(json.dumps(result["resources"], ensure_ascii=False, sort_keys=True))
    if "prompts" in result:
        parts.append(json.dumps(result["prompts"], ensure_ascii=False, sort_keys=True))
    if "messages" in result:
        parts.append(json.dumps(result["messages"], ensure_ascii=False, sort_keys=True))
    return "\n".join(part for part in parts if part) or json.dumps(result, ensure_ascii=False, sort_keys=True)


class MCPToolAdapter:
    def __init__(self, client: Any, prefix: str, tool_def: dict[str, Any]) -> None:
        self.client = client
        self.prefix = prefix
        self.mcp_tool_name = str(tool_def.get("name", "tool"))
        self.name = f"{prefix}_{self.mcp_tool_name}"
        description = str(tool_def.get("description") or "MCP tool")
        self.description = f"[MCP:{client.name}] {description}"
        self.input_schema = _object_or_default(tool_def.get("inputSchema"))

    def run(self, arguments: dict[str, Any]) -> str:
        return format_mcp_result(self.client.call_tool(self.mcp_tool_name, arguments))


class MCPListResourcesTool:
    def __init__(self, client: Any, prefix: str) -> None:
        self.client = client
        self.name = f"{prefix}_list_resources"
        self.description = f"[MCP:{client.name}] 列出 MCP resources"
        self.input_schema = {"type": "object", "properties": {}}

    def run(self, arguments: dict[str, Any]) -> str:
        return format_mcp_result(self.client.list_resources())


class MCPReadResourceTool:
    def __init__(self, client: Any, prefix: str) -> None:
        self.client = client
        self.name = f"{prefix}_read_resource"
        self.description = f"[MCP:{client.name}] 读取 MCP resource"
        self.input_schema = {
            "type": "object",
            "properties": {"uri": {"type": "string", "description": "Resource URI"}},
            "required": ["uri"],
        }

    def run(self, arguments: dict[str, Any]) -> str:
        return format_mcp_result(self.client.read_resource(str(arguments.get("uri", ""))))


class MCPListPromptsTool:
    def __init__(self, client: Any, prefix: str) -> None:
        self.client = client
        self.name = f"{prefix}_list_prompts"
        self.description = f"[MCP:{client.name}] 列出 MCP prompts"
        self.input_schema = {"type": "object", "properties": {}}

    def run(self, arguments: dict[str, Any]) -> str:
        return format_mcp_result(self.client.list_prompts())


class MCPGetPromptTool:
    def __init__(self, client: Any, prefix: str) -> None:
        self.client = client
        self.name = f"{prefix}_get_prompt"
        self.description = f"[MCP:{client.name}] 获取 MCP prompt"
        self.input_schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Prompt name"},
                "arguments": {"type": "object", "description": "Prompt arguments"},
            },
            "required": ["name"],
        }

    def run(self, arguments: dict[str, Any]) -> str:
        return format_mcp_result(
            self.client.get_prompt(str(arguments.get("name", "")), _object_or_default(arguments.get("arguments")))
        )


def tools_for_client(client: Any, prefix: str, disabled_tools: list[str] | tuple[str, ...] = ()) -> list[Any]:
    disabled = set(disabled_tools)
    tools: list[Any] = []
    for tool_def in getattr(client, "tools", []):
        name = tool_def.get("name")
        if isinstance(name, str) and name not in disabled:
            tools.append(MCPToolAdapter(client, prefix, tool_def))
    if getattr(client, "resources", []):
        tools.append(MCPListResourcesTool(client, prefix))
        tools.append(MCPReadResourceTool(client, prefix))
    if getattr(client, "prompts", []):
        tools.append(MCPListPromptsTool(client, prefix))
        tools.append(MCPGetPromptTool(client, prefix))
    return tools


def _format_resource(resource: Any) -> str:
    if not isinstance(resource, dict):
        return str(resource)
    uri = resource.get("uri", "")
    mime_type = resource.get("mimeType", "")
    text = resource.get("text")
    blob = f"resource uri={uri} mimeType={mime_type}".strip()
    if isinstance(text, str):
        return f"{blob}\n{text}"
    return blob


def _object_or_default(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {"type": "object", "properties": {}}
