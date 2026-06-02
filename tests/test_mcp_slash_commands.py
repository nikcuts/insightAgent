from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from insightagent.agent import CodeAgent
from insightagent.messages import Message, ModelResponse
from insightagent.providers import ModelClient
from insightagent.slash_commands import SlashCommandProcessor
from insightagent.tool_context import ToolContext
from insightagent.tools import ToolRegistry


class FakeModelClient(ModelClient):
    def complete(self, messages: list[Message], tools: list[dict]) -> ModelResponse:
        return ModelResponse(content="done")


class FakeTool:
    name = "mcp_fake_echo"
    description = "fake"
    input_schema = {"type": "object", "properties": {}}

    def run(self, arguments: dict[str, Any]) -> str:
        return "ok"


class FakeMCPManager:
    def __init__(self) -> None:
        self.restarted: list[str] = []
        self.refreshed: list[str] = []

    def status(self) -> dict[str, dict[str, Any]]:
        return {
            "fake": {
                "state": "running",
                "transport": "stdio",
                "tools": 1,
                "resources": 2,
                "prompts": 3,
                "last_error": "",
            }
        }

    def get_tools(self) -> list[FakeTool]:
        return [FakeTool()]

    def restart_server(self, name: str) -> bool:
        self.restarted.append(name)
        return name == "fake"

    def refresh_server(self, name: str) -> bool:
        self.refreshed.append(name)
        return name == "fake"


def make_agent(workspace: Path) -> CodeAgent:
    return CodeAgent(FakeModelClient(), tools=ToolRegistry(context=ToolContext(workspace=workspace)))


class MCPSlashCommandTests(unittest.TestCase):
    def test_mcp_unavailable_without_manager(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            slash = SlashCommandProcessor(make_agent(Path(directory)))

            self.assertEqual(slash.handle("/mcp status"), "MCP unavailable")

    def test_mcp_status_tools_restart_and_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = FakeMCPManager()
            slash = SlashCommandProcessor(make_agent(Path(directory)), mcp_manager=manager)

            status = slash.handle("/mcp status")
            tools = slash.handle("/mcp tools")
            restart = slash.handle("/mcp restart fake")
            refresh = slash.handle("/mcp refresh fake")

        self.assertIn("fake", status)
        self.assertIn("running", status)
        self.assertIn("mcp_fake_echo", tools)
        self.assertEqual(restart, "MCP server restarted: fake")
        self.assertEqual(refresh, "MCP server refreshed: fake")
        self.assertEqual(manager.restarted, ["fake"])
        self.assertEqual(manager.refreshed, ["fake"])


if __name__ == "__main__":
    unittest.main()
