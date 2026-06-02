from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any

from insightagent.mcp.client import MCPClient
from insightagent.mcp.config import MCPServerConfig


class FakeUnsupportedProtocolTransport:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def send_request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float = 60,
    ) -> dict[str, Any]:
        return {
            "protocolVersion": "1900-01-01",
            "capabilities": {},
            "serverInfo": {"name": "old", "version": "0"},
        }

    def send_notification(self, method: str, params: dict[str, Any] | None = None) -> None:
        pass

    def stderr_summary(self) -> str:
        return ""


class MCPClientTests(unittest.TestCase):
    def test_client_starts_and_refreshes_capabilities(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "fake_mcp_stdio_server.py"
        config = MCPServerConfig.from_dict("fake", {"command": sys.executable, "args": [str(fixture)]})
        client = MCPClient(config)
        try:
            client.start()

            self.assertEqual(client.state, "running")
            self.assertEqual(client.protocol_version, "2025-06-18")
            self.assertEqual(client.tools[0]["name"], "echo")
            self.assertEqual(client.resources[0]["uri"], "fake://note")
            self.assertEqual(client.prompts[0]["name"], "review")
            self.assertEqual(client.status()["tools"], 1)
        finally:
            client.stop()

        self.assertEqual(client.state, "stopped")

    def test_client_calls_tool_resource_and_prompt(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "fake_mcp_stdio_server.py"
        config = MCPServerConfig.from_dict("fake", {"command": sys.executable, "args": [str(fixture)]})
        client = MCPClient(config)
        try:
            client.start()

            tool_result = client.call_tool("echo", {"text": "hi"})
            resource_result = client.read_resource("fake://note")
            prompt_result = client.get_prompt("review", {})

        finally:
            client.stop()

        self.assertEqual(tool_result["content"][0]["text"], "echo: hi")
        self.assertEqual(resource_result["contents"][0]["text"], "hello resource")
        self.assertEqual(prompt_result["messages"][0]["content"]["text"], "review this")

    def test_unsupported_protocol_marks_client_failed(self) -> None:
        config = MCPServerConfig.from_dict("fake", {"command": "python3"})
        transport = FakeUnsupportedProtocolTransport()
        client = MCPClient(config, transport=transport)

        with self.assertRaises(Exception):
            client.start()

        self.assertEqual(client.state, "failed")
        self.assertIn("unsupported", client.last_error)
        self.assertTrue(transport.stopped)


if __name__ == "__main__":
    unittest.main()
