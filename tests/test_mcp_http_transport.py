from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from insightagent.mcp.errors import MCPTransportError
from insightagent.mcp.transports import StreamableHttpTransport


class FakeMCPHTTPHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, Any]] = []
    response_mode = "json"
    force_404 = False

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        FakeMCPHTTPHandler.requests.append({"headers": dict(self.headers), "body": body})
        if FakeMCPHTTPHandler.force_404:
            self.send_response(404)
            self.end_headers()
            return
        message_id = body.get("id")
        method = body.get("method")
        if method == "initialize":
            result = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-http", "version": "1"},
            }
        elif method == "tools/list":
            result = {"tools": [{"name": "echo", "description": "Echo", "inputSchema": {"type": "object"}}]}
        else:
            result = {}
        response = {"jsonrpc": "2.0", "id": message_id, "result": result}
        payload = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("Mcp-Session-Id", "session-123")
        if FakeMCPHTTPHandler.response_mode == "sse":
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b"event: message\n")
            self.wfile.write(b"data: " + payload + b"\n\n")
        else:
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload)

    def log_message(self, format: str, *args: Any) -> None:
        return


def _header(headers: dict[str, str], name: str) -> str:
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    raise KeyError(name)


class MCPHttpTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeMCPHTTPHandler.requests = []
        FakeMCPHTTPHandler.response_mode = "json"
        FakeMCPHTTPHandler.force_404 = False
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeMCPHTTPHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/mcp"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def test_http_transport_sends_headers_and_tracks_session(self) -> None:
        transport = StreamableHttpTransport(
            "fake",
            self.url,
            headers={"Authorization": "Bearer token"},
            request_timeout=5,
        )

        init = transport.send_request("initialize", {})
        transport.set_protocol_version(init["protocolVersion"])
        tools = transport.send_request("tools/list")

        self.assertEqual(tools["tools"][0]["name"], "echo")
        first_headers = FakeMCPHTTPHandler.requests[0]["headers"]
        second_headers = FakeMCPHTTPHandler.requests[1]["headers"]
        self.assertEqual(_header(first_headers, "Authorization"), "Bearer token")
        self.assertIn("application/json", _header(first_headers, "Accept"))
        self.assertEqual(_header(second_headers, "MCP-Protocol-Version"), "2025-06-18")
        self.assertEqual(_header(second_headers, "Mcp-Session-Id"), "session-123")

    def test_http_transport_reads_sse_response(self) -> None:
        FakeMCPHTTPHandler.response_mode = "sse"
        transport = StreamableHttpTransport("fake", self.url, request_timeout=5)

        result = transport.send_request("tools/list")

        self.assertEqual(result["tools"][0]["name"], "echo")

    def test_http_404_with_active_session_clears_session_and_raises(self) -> None:
        transport = StreamableHttpTransport("fake", self.url, request_timeout=5)
        transport.send_request("initialize", {})

        FakeMCPHTTPHandler.force_404 = True
        with self.assertRaises(MCPTransportError):
            transport.send_request("tools/list")

        self.assertIsNone(transport.session_id)


if __name__ == "__main__":
    unittest.main()
