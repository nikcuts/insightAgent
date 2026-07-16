from __future__ import annotations

import asyncio
from pathlib import Path

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel

from insightagent.mcp.adapters import MCPToolRuntime, mcp_success_payload
from insightagent.runtime.tool_context import ToolContext


class EchoArguments(BaseModel):
    text: str


def test_official_tool_wrapper_preserves_langchain_schema_and_result(tmp_path: Path) -> None:
    source = StructuredTool(
        name="echo",
        description="Echo text.",
        args_schema=EchoArguments,
        coroutine=lambda text: asyncio.sleep(0, result=f"echo: {text}"),
        metadata={"readOnlyHint": True},
    )
    invoked: list[dict[str, object]] = []

    async def call(source_tool, spec, arguments, config):
        del config
        invoked.append(arguments)
        return mcp_success_payload(spec, arguments, await source_tool.ainvoke(arguments))

    wrapped, spec = MCPToolRuntime(ToolContext(workspace=tmp_path), "demo", call).wrap(
        source, "mcp_demo_echo"
    )

    result = asyncio.run(wrapped.ainvoke({"text": "hello"}))

    assert isinstance(wrapped, BaseTool)
    assert wrapped.name == "mcp_demo_echo"
    assert spec.mcp_server == "demo"
    assert spec.required_permission.value == "read"
    assert invoked == [{"text": "hello"}]
    assert result["content"] == "echo: hello"
