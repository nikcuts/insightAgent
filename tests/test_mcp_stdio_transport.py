from __future__ import annotations

import sys
import unittest
from pathlib import Path

from insightagent.mcp.protocol import SUPPORTED_PROTOCOL_VERSION
from insightagent.mcp.transports import StdioTransport


class MCPStdioTransportTests(unittest.TestCase):
    def test_stdio_transport_sends_requests_and_notifications(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "fake_mcp_stdio_server.py"
        transport = StdioTransport("fake", sys.executable, [str(fixture)])
        try:
            transport.start()

            init = transport.send_request(
                "initialize",
                {
                    "protocolVersion": SUPPORTED_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
                timeout=5,
            )
            transport.send_notification("notifications/initialized")
            tools = transport.send_request("tools/list", timeout=5)

            self.assertEqual(init["protocolVersion"], SUPPORTED_PROTOCOL_VERSION)
            self.assertEqual(tools["tools"][0]["name"], "echo")
            self.assertTrue(transport.is_running())
            self.assertIn("fake stderr booted", transport.stderr_summary())
        finally:
            transport.stop()

        self.assertFalse(transport.is_running())


if __name__ == "__main__":
    unittest.main()
