from __future__ import annotations

import json
import sys


TOOLS = [
    {
        "name": "echo",
        "description": "Echo text",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    }
]


def send(message: dict) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


sys.stderr.write("fake stderr booted\n")
sys.stderr.flush()

for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    message_id = message.get("id")
    if method == "initialize":
        send(
            {
                "jsonrpc": "2.0",
                "id": message_id,
                "result": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
                    "serverInfo": {"name": "fake-stdio", "version": "1.0.0"},
                    "instructions": "fake server for tests",
                },
            }
        )
    elif method == "notifications/initialized":
        continue
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": message_id, "result": {"tools": TOOLS}})
    elif method == "resources/list":
        send(
            {
                "jsonrpc": "2.0",
                "id": message_id,
                "result": {"resources": [{"uri": "fake://note", "name": "note", "mimeType": "text/plain"}]},
            }
        )
    elif method == "prompts/list":
        send(
            {
                "jsonrpc": "2.0",
                "id": message_id,
                "result": {"prompts": [{"name": "review", "description": "Review prompt"}]},
            }
        )
    elif method == "resources/read":
        send(
            {
                "jsonrpc": "2.0",
                "id": message_id,
                "result": {"contents": [{"uri": "fake://note", "mimeType": "text/plain", "text": "hello resource"}]},
            }
        )
    elif method == "prompts/get":
        send(
            {
                "jsonrpc": "2.0",
                "id": message_id,
                "result": {
                    "description": "Review prompt",
                    "messages": [{"role": "user", "content": {"type": "text", "text": "review this"}}],
                },
            }
        )
    elif method == "tools/call":
        args = message.get("params", {}).get("arguments", {})
        send(
            {
                "jsonrpc": "2.0",
                "id": message_id,
                "result": {"content": [{"type": "text", "text": "echo: " + args.get("text", "")}]},
            }
        )
    else:
        send({"jsonrpc": "2.0", "id": message_id, "error": {"code": -32601, "message": method}})
