from __future__ import annotations

import unittest

from insightagent.mcp.errors import MCPProtocolError
from insightagent.mcp.protocol import (
    SUPPORTED_PROTOCOL_VERSION,
    JsonRpcIdGenerator,
    build_notification,
    build_request,
    parse_response,
)


class MCPProtocolTests(unittest.TestCase):
    def test_build_request_and_notification(self) -> None:
        ids = JsonRpcIdGenerator()

        request = build_request(ids.next(), "initialize", {"protocolVersion": SUPPORTED_PROTOCOL_VERSION})
        notification = build_notification("notifications/initialized")

        self.assertEqual(request["jsonrpc"], "2.0")
        self.assertEqual(request["id"], 1)
        self.assertEqual(request["method"], "initialize")
        self.assertEqual(notification, {"jsonrpc": "2.0", "method": "notifications/initialized"})

    def test_parse_success_response(self) -> None:
        result = parse_response({"jsonrpc": "2.0", "id": 7, "result": {"ok": True}}, expected_id=7)

        self.assertEqual(result, {"ok": True})

    def test_parse_error_response_raises(self) -> None:
        with self.assertRaises(MCPProtocolError) as context:
            parse_response(
                {
                    "jsonrpc": "2.0",
                    "id": 7,
                    "error": {"code": -32601, "message": "method not found"},
                },
                expected_id=7,
            )

        self.assertIn("method not found", str(context.exception))

    def test_parse_rejects_mismatched_id(self) -> None:
        with self.assertRaises(MCPProtocolError):
            parse_response({"jsonrpc": "2.0", "id": 8, "result": {}}, expected_id=7)


if __name__ == "__main__":
    unittest.main()
