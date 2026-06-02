from __future__ import annotations

import unittest
from typing import Any

from insightagent.mcp.config import MCPConfig, MCPServerConfig
from insightagent.mcp.manager import MCPManager


class FakeClient:
    instances: dict[str, "FakeClient"] = {}

    def __init__(self, config: MCPServerConfig) -> None:
        self.server_config = config
        self.name = config.name
        self.state = "created"
        self.started = 0
        self.stopped = 0
        self.tools = [
            {
                "name": "echo",
                "description": "Echo",
                "inputSchema": {"type": "object", "properties": {}},
            }
        ]
        self.resources: list[dict[str, Any]] = []
        self.prompts: list[dict[str, Any]] = []
        FakeClient.instances[self.name] = self

    def start(self) -> None:
        self.started += 1
        if self.name == "bad":
            self.state = "failed"
            raise RuntimeError("boom")
        self.state = "running"

    def stop(self) -> None:
        self.stopped += 1
        self.state = "stopped"

    def refresh_capabilities(self) -> None:
        self.tools.append(
            {
                "name": "fresh",
                "description": "Fresh",
                "inputSchema": {"type": "object", "properties": {}},
            }
        )

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": name}]}

    def status(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state,
            "transport": self.server_config.transport,
            "tools": len(self.tools),
            "resources": 0,
            "prompts": 0,
            "last_error": "",
            "stderr": "",
        }


def make_config() -> MCPConfig:
    return MCPConfig(
        servers={
            "good": MCPServerConfig.from_dict("good", {"command": "python3"}),
            "bad": MCPServerConfig.from_dict("bad", {"command": "python3"}),
            "disabled": MCPServerConfig.from_dict("disabled", {"command": "python3", "enabled": False}),
        }
    )


class MCPManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeClient.instances = {}

    def test_start_enabled_skips_disabled_and_keeps_failures_isolated(self) -> None:
        manager = MCPManager(make_config(), client_factory=FakeClient)

        started = manager.start_enabled()
        status = manager.status()

        self.assertEqual(started, 1)
        self.assertIn("good", manager.clients)
        self.assertNotIn("disabled", manager.clients)
        self.assertEqual(status["bad"]["state"], "failed")
        self.assertIn("boom", status["bad"]["last_error"])

    def test_get_tools_and_conflict_reporting(self) -> None:
        config = MCPConfig(
            servers={
                "one": MCPServerConfig.from_dict("one", {"command": "python3", "tool_prefix": "mcp_same"}),
                "two": MCPServerConfig.from_dict("two", {"command": "python3", "tool_prefix": "mcp_same"}),
            }
        )
        manager = MCPManager(config, client_factory=FakeClient)

        manager.start_enabled()
        tools = manager.get_tools()
        status = manager.status()

        self.assertEqual([tool.name for tool in tools], ["mcp_same_echo"])
        self.assertIn("duplicate tool name", status["two"]["last_error"])

    def test_restart_and_refresh_server(self) -> None:
        manager = MCPManager(make_config(), client_factory=FakeClient)
        manager.start_enabled()
        old_client = FakeClient.instances["good"]

        self.assertTrue(manager.restart_server("good"))
        new_client = FakeClient.instances["good"]
        manager.refresh_server("good")

        self.assertEqual(old_client.stopped, 1)
        self.assertEqual(new_client.started, 1)
        self.assertIn("mcp_good_fresh", {tool.name for tool in manager.get_tools()})


if __name__ == "__main__":
    unittest.main()
