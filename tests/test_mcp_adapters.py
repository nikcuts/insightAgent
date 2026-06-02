from __future__ import annotations

import unittest
from typing import Any

from insightagent.mcp.adapters import (
    MCPGetPromptTool,
    MCPListPromptsTool,
    MCPListResourcesTool,
    MCPReadResourceTool,
    MCPToolAdapter,
    format_mcp_result,
    tools_for_client,
)


class FakeClient:
    name = "fake"
    tools = [
        {
            "name": "echo",
            "description": "Echo text",
            "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
        }
    ]
    resources = [{"uri": "fake://note", "name": "note", "mimeType": "text/plain"}]
    prompts = [{"name": "review", "description": "Review prompt"}]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if arguments.get("error"):
            return {"isError": True, "content": [{"type": "text", "text": "bad call"}]}
        return {
            "content": [{"type": "text", "text": f"{name}: {arguments.get('text', '')}"}],
            "structuredContent": {"ok": True},
        }

    def list_resources(self) -> dict[str, Any]:
        return {"resources": self.resources}

    def read_resource(self, uri: str) -> dict[str, Any]:
        return {"contents": [{"uri": uri, "mimeType": "text/plain", "text": "hello resource"}]}

    def list_prompts(self) -> dict[str, Any]:
        return {"prompts": self.prompts}

    def get_prompt(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"messages": [{"role": "user", "content": {"type": "text", "text": f"prompt {name}"}}]}


class MCPAdapterTests(unittest.TestCase):
    def test_tool_adapter_maps_schema_and_formats_result(self) -> None:
        adapter = MCPToolAdapter(FakeClient(), "mcp_fake", FakeClient.tools[0])

        result = adapter.run({"text": "hi"})

        self.assertEqual(adapter.name, "mcp_fake_echo")
        self.assertEqual(adapter.input_schema["type"], "object")
        self.assertIn("[MCP:fake]", adapter.description)
        self.assertIn("echo: hi", result)
        self.assertIn('"ok": true', result)

    def test_tool_adapter_marks_error_results(self) -> None:
        adapter = MCPToolAdapter(FakeClient(), "mcp_fake", FakeClient.tools[0])

        result = adapter.run({"error": True})

        self.assertIn("MCP tool error", result)
        self.assertIn("bad call", result)

    def test_resource_and_prompt_adapters(self) -> None:
        client = FakeClient()

        resources = MCPListResourcesTool(client, "mcp_fake").run({})
        resource = MCPReadResourceTool(client, "mcp_fake").run({"uri": "fake://note"})
        prompts = MCPListPromptsTool(client, "mcp_fake").run({})
        prompt = MCPGetPromptTool(client, "mcp_fake").run({"name": "review", "arguments": {}})

        self.assertIn("fake://note", resources)
        self.assertIn("hello resource", resource)
        self.assertIn("review", prompts)
        self.assertIn("prompt review", prompt)

    def test_tools_for_client_includes_tools_resources_and_prompts(self) -> None:
        tools = tools_for_client(FakeClient(), "mcp_fake")
        names = {tool.name for tool in tools}

        self.assertIn("mcp_fake_echo", names)
        self.assertIn("mcp_fake_list_resources", names)
        self.assertIn("mcp_fake_read_resource", names)
        self.assertIn("mcp_fake_list_prompts", names)
        self.assertIn("mcp_fake_get_prompt", names)

    def test_format_mcp_result_handles_resource_content(self) -> None:
        result = format_mcp_result(
            {
                "content": [
                    {
                        "type": "resource",
                        "resource": {"uri": "file://x", "mimeType": "text/plain", "text": "abc"},
                    }
                ]
            }
        )

        self.assertIn("file://x", result)
        self.assertIn("abc", result)


if __name__ == "__main__":
    unittest.main()
