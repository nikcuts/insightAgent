# MCP Runtime Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 InsightAgent V5.0 增加成品化 MCP Runtime Layer，支持 `stdio`、`streamable_http`、tools、resources、prompts、CLI `/mcp` 可观测能力和中文文档。

**Architecture:** 新增 `insightagent/mcp/` 子系统，负责配置、JSON-RPC 协议、transport、client、manager 和 adapter。现有 agent loop 不重写，MCP 能力统一适配为当前 `Tool` protocol，再注入 `ToolRegistry`。

**Tech Stack:** Python 标准库、`unittest`、`subprocess`、`threading`、`queue`、`http.server`、`urllib.request`、JSON-RPC 2.0、MCP 2025-06-18。

---

## File Structure

- Create: `insightagent/mcp/__init__.py`，导出 MCP 子系统公开 API。
- Create: `insightagent/mcp/errors.py`，定义 MCP 专用异常。
- Create: `insightagent/mcp/config.py`，读取、合并、校验 MCP 配置并脱敏。
- Create: `insightagent/mcp/protocol.py`，构建和解析 JSON-RPC request、notification、response。
- Create: `insightagent/mcp/transports.py`，实现 `StdioTransport`、`StreamableHttpTransport` 和测试用 fake transport 友好的接口。
- Create: `insightagent/mcp/client.py`，实现单 server MCP client。
- Create: `insightagent/mcp/adapters.py`，把 tools/resources/prompts 包装为 InsightAgent `Tool`。
- Create: `insightagent/mcp/manager.py`，管理多 server 生命周期、工具收集、状态和重启。
- Modify: `insightagent/tools/registry.py`，支持向默认工具追加外部工具并处理名称冲突。
- Modify: `insightagent/run_task.py`，启动和关闭 MCP manager，并将 MCP tools 注入 registry。
- Modify: `insightagent/cli.py`，启动和关闭 MCP manager，把 manager 传给 slash command。
- Modify: `insightagent/slash_commands.py`，增加 `/mcp status|tools|restart|refresh`。
- Modify: `insightagent/trace.py`，渲染 MCP trace event。
- Create: `tests/fixtures/fake_mcp_stdio_server.py`，fake stdio MCP server。
- Create: `tests/test_mcp_config.py`。
- Create: `tests/test_mcp_protocol.py`。
- Create: `tests/test_mcp_stdio_transport.py`。
- Create: `tests/test_mcp_http_transport.py`。
- Create: `tests/test_mcp_client.py`。
- Create: `tests/test_mcp_adapters.py`。
- Create: `tests/test_mcp_manager.py`。
- Create: `tests/test_mcp_slash_commands.py`。
- Modify: `tests/test_extended_tools.py`，覆盖外部工具追加和冲突行为。
- Modify: `tests/test_slash_commands.py`，确认 `/help` 包含 `/mcp`。
- Create: `MCP_GUIDE.md`，中文 MCP 使用指南。
- Create: `mcp_config.json.example`，中文注释不可用于 JSON，因此用示例值表达。
- Modify: `README.md`，中文更新 MCP 能力、架构和命令。

## Task 1: MCP Config

**Files:**
- Create: `insightagent/mcp/__init__.py`
- Create: `insightagent/mcp/errors.py`
- Create: `insightagent/mcp/config.py`
- Test: `tests/test_mcp_config.py`

- [ ] **Step 1: Write failing config tests**

Create `tests/test_mcp_config.py`:

```python
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from insightagent.mcp.config import (
    MCPConfig,
    MCPServerConfig,
    expand_env_value,
    load_mcp_config,
    redact_mapping,
)
from insightagent.mcp.errors import MCPConfigError


class MCPConfigTests(unittest.TestCase):
    def test_loads_and_merges_mcp_config_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            workspace = root / "workspace"
            (home / ".insightagent").mkdir(parents=True)
            (workspace / ".insightagent").mkdir(parents=True)
            (home / ".insightagent" / "mcp_config.json").write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "playwright": {
                                "command": "npx",
                                "args": ["old"],
                                "enabled": True,
                            },
                            "global-only": {
                                "transport": "streamable_http",
                                "url": "https://example.test/mcp",
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )
            (workspace / ".insightagent" / "mcp_config.json").write_text(
                json.dumps({"mcpServers": {"playwright": {"args": ["new"], "enabled": False}}}),
                encoding="utf-8",
            )
            (workspace / "mcp_config.json").write_text(
                json.dumps({"mcpServers": {"local": {"command": "python3", "args": ["server.py"]}}}),
                encoding="utf-8",
            )

            config = load_mcp_config(workspace, user_config_home=home / ".insightagent")

        self.assertEqual(config.servers["playwright"].command, "npx")
        self.assertEqual(config.servers["playwright"].args, ["new"])
        self.assertFalse(config.servers["playwright"].enabled)
        self.assertEqual(config.servers["global-only"].transport, "streamable_http")
        self.assertEqual(config.servers["local"].tool_prefix, "mcp_local")
        self.assertEqual(len(config.loaded_files), 3)

    def test_validates_required_fields_by_transport(self) -> None:
        with self.assertRaises(MCPConfigError):
            MCPServerConfig.from_dict("bad-stdio", {"transport": "stdio"})
        with self.assertRaises(MCPConfigError):
            MCPServerConfig.from_dict("bad-http", {"transport": "streamable_http"})
        with self.assertRaises(MCPConfigError):
            MCPServerConfig.from_dict("bad-kind", {"transport": "sse", "url": "x"})

    def test_env_expansion_and_redaction(self) -> None:
        env = {"TOKEN": "secret-token"}
        self.assertEqual(expand_env_value("Bearer ${TOKEN}", env), "Bearer secret-token")
        self.assertEqual(expand_env_value("${MISSING}", env), "")

        redacted = redact_mapping(
            {
                "Authorization": "Bearer secret-token",
                "DEBUG": "true",
                "API_KEY": "abc",
            }
        )

        self.assertEqual(redacted["Authorization"], "<redacted>")
        self.assertEqual(redacted["API_KEY"], "<redacted>")
        self.assertEqual(redacted["DEBUG"], "true")

    def test_empty_config_when_no_files_exist(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = load_mcp_config(Path(directory), user_config_home=Path(directory) / "home")

        self.assertIsInstance(config, MCPConfig)
        self.assertEqual(config.servers, {})


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run config tests and verify failure**

Run:

```bash
python3 -m unittest tests.test_mcp_config -v
```

Expected: FAIL with `ModuleNotFoundError` or missing `insightagent.mcp.config`.

- [ ] **Step 3: Implement MCP config**

Create `insightagent/mcp/errors.py`:

```python
"""MCP-specific errors."""

from __future__ import annotations


class MCPError(Exception):
    """Base class for MCP runtime errors."""


class MCPConfigError(MCPError):
    """Raised when MCP configuration is invalid."""


class MCPTransportError(MCPError):
    """Raised when MCP transport startup or I/O fails."""


class MCPProtocolError(MCPError):
    """Raised when JSON-RPC or MCP protocol handling fails."""


class MCPRequestTimeout(MCPTransportError):
    """Raised when an MCP request times out."""


class MCPToolError(MCPError):
    """Raised when an MCP tool call returns an error."""
```

Create `insightagent/mcp/config.py` with dataclasses `MCPServerConfig`, `MCPConfig`, helpers `_deep_merge`, `expand_env_value`, `expand_mapping`, `redact_mapping`, `load_mcp_config`.

Implementation requirements:

- `transport` defaults to `stdio`.
- `stdio` requires `command`.
- `streamable_http` requires `url`.
- `tool_prefix` defaults to `mcp_<server_name_with_non_alnum_as_underscore>`.
- `enabled` defaults to `True`.
- `args` defaults to `[]`.
- `env`, `headers`, `disabled_tools` default to empty mappings/lists.
- Load order: `user_config_home/mcp_config.json`, `workspace/.insightagent/mcp_config.json`, `workspace/mcp_config.json`.
- `user_config_home` means the actual `.insightagent` directory, not home root.
- Merge same server dictionaries deeply.
- JSON parse or shape errors raise `MCPConfigError`.

Create `insightagent/mcp/__init__.py` exporting `MCPConfig`, `MCPServerConfig`, `load_mcp_config`.

- [ ] **Step 4: Run config tests and commit**

Run:

```bash
python3 -m unittest tests.test_mcp_config -v
```

Expected: PASS.

Commit:

```bash
git add insightagent/mcp/__init__.py insightagent/mcp/errors.py insightagent/mcp/config.py tests/test_mcp_config.py
git commit -m "Add MCP config loading"
```

## Task 2: JSON-RPC Protocol

**Files:**
- Create: `insightagent/mcp/protocol.py`
- Test: `tests/test_mcp_protocol.py`

- [ ] **Step 1: Write failing protocol tests**

Create `tests/test_mcp_protocol.py`:

```python
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
```

- [ ] **Step 2: Run protocol tests and verify failure**

Run:

```bash
python3 -m unittest tests.test_mcp_protocol -v
```

Expected: FAIL because `insightagent.mcp.protocol` does not exist.

- [ ] **Step 3: Implement protocol helpers**

Create `insightagent/mcp/protocol.py`:

```python
"""JSON-RPC helpers for MCP."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .errors import MCPProtocolError


SUPPORTED_PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = {SUPPORTED_PROTOCOL_VERSION}


@dataclass
class JsonRpcIdGenerator:
    current: int = 0

    def next(self) -> int:
        self.current += 1
        return self.current


def build_request(message_id: int, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": message_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def build_notification(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        message["params"] = params
    return message


def parse_response(message: dict[str, Any], expected_id: int) -> Any:
    if message.get("jsonrpc") != "2.0":
        raise MCPProtocolError(f"invalid JSON-RPC version: {message.get('jsonrpc')}")
    if message.get("id") != expected_id:
        raise MCPProtocolError(f"unexpected response id: {message.get('id')} expected={expected_id}")
    if "error" in message:
        error = message["error"]
        if isinstance(error, dict):
            raise MCPProtocolError(f"MCP error {error.get('code')}: {error.get('message')}")
        raise MCPProtocolError(f"MCP error: {error}")
    if "result" not in message:
        raise MCPProtocolError("JSON-RPC response missing result")
    return message["result"]
```

- [ ] **Step 4: Run protocol tests and commit**

Run:

```bash
python3 -m unittest tests.test_mcp_protocol -v
```

Expected: PASS.

Commit:

```bash
git add insightagent/mcp/protocol.py tests/test_mcp_protocol.py
git commit -m "Add MCP JSON-RPC protocol helpers"
```

## Task 3: Stdio Transport

**Files:**
- Create: `insightagent/mcp/transports.py`
- Create: `tests/fixtures/fake_mcp_stdio_server.py`
- Test: `tests/test_mcp_stdio_transport.py`

- [ ] **Step 1: Write fake server and failing stdio transport tests**

Create `tests/fixtures/fake_mcp_stdio_server.py`:

```python
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
                "result": {"description": "Review prompt", "messages": [{"role": "user", "content": {"type": "text", "text": "review this"}}]},
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
```

Create `tests/test_mcp_stdio_transport.py` with tests that start `StdioTransport("fake", sys.executable, [fixture])`, call `send_request("initialize", ...)`, send notification, call `send_request("tools/list")`, and assert stderr summary is string and process stops.

- [ ] **Step 2: Run stdio transport tests and verify failure**

Run:

```bash
python3 -m unittest tests.test_mcp_stdio_transport -v
```

Expected: FAIL because `StdioTransport` does not exist.

- [ ] **Step 3: Implement StdioTransport**

Implement in `insightagent/mcp/transports.py`:

- `BaseTransport` protocol-like base with `start`, `stop`, `send_request`, `send_notification`, `stderr_summary`.
- `StdioTransport`:
  - starts subprocess with text mode and line buffering.
  - merges `os.environ` and config env.
  - background stdout reader puts parsed JSON lines into queue.
  - background stderr reader stores last 20 lines in `collections.deque`.
  - `send_request(method, params, timeout)` writes request, waits for matching id, ignores notifications.
  - `send_notification(method, params)` writes notification.
  - `stop()` closes/terminates process safely.

- [ ] **Step 4: Run stdio transport tests and commit**

Run:

```bash
python3 -m unittest tests.test_mcp_stdio_transport -v
```

Expected: PASS.

Commit:

```bash
git add insightagent/mcp/transports.py tests/fixtures/fake_mcp_stdio_server.py tests/test_mcp_stdio_transport.py
git commit -m "Add MCP stdio transport"
```

## Task 4: MCP Client

**Files:**
- Create: `insightagent/mcp/client.py`
- Test: `tests/test_mcp_client.py`

- [ ] **Step 1: Write failing client tests**

Create `tests/test_mcp_client.py` using the fake stdio fixture:

- test `start()` initializes, sends initialized notification, refreshes tools/resources/prompts.
- test `call_tool("echo", {"text": "hi"})` returns MCP result dict.
- test `read_resource("fake://note")` returns contents.
- test `get_prompt("review", {})` returns prompt messages.
- test unsupported protocol version marks client failed using a small fake transport class.

- [ ] **Step 2: Run client tests and verify failure**

Run:

```bash
python3 -m unittest tests.test_mcp_client -v
```

Expected: FAIL because `MCPClient` does not exist.

- [ ] **Step 3: Implement MCPClient**

Implement:

- `MCPClientStatus` as string constants or enum: `created`, `starting`, `initialized`, `running`, `stopped`, `failed`.
- `MCPClient.__init__(server_config, transport=None)`.
- Build transport from config if not provided.
- `start()`:
  - starts transport.
  - sends `initialize` with protocol version, client info `insightagent`.
  - validates protocol version.
  - sends `notifications/initialized`.
  - calls `refresh_capabilities()`.
  - status becomes `running`.
- `refresh_capabilities()`:
  - calls `tools/list`, `resources/list`, `prompts/list`.
  - stores empty lists if methods fail with method-not-found style protocol errors.
- method wrappers for tools/resources/prompts.
- `status()` returns dict with name, status, transport, counts, last_error, stderr summary.

- [ ] **Step 4: Run client tests and commit**

Run:

```bash
python3 -m unittest tests.test_mcp_client -v
```

Expected: PASS.

Commit:

```bash
git add insightagent/mcp/client.py tests/test_mcp_client.py
git commit -m "Add MCP client lifecycle"
```

## Task 5: Adapters and Registry Extension

**Files:**
- Create: `insightagent/mcp/adapters.py`
- Modify: `insightagent/tools/registry.py`
- Test: `tests/test_mcp_adapters.py`
- Modify Test: `tests/test_extended_tools.py`

- [ ] **Step 1: Write failing adapter and registry tests**

Create `tests/test_mcp_adapters.py`:

- fake client with `tools`, `resources`, `prompts`, and methods returning sample dicts.
- assert `MCPToolAdapter.name == "mcp_fake_echo"`.
- assert tool result text content becomes string.
- assert `structuredContent` appears as JSON.
- assert `isError=true` result contains `MCP tool error`.
- assert resource/prompt adapters produce list/read/get tools.

Modify `tests/test_extended_tools.py`:

- add a tiny `ExternalTool` class.
- test `ToolRegistry(tools=default_tools(context) + [ExternalTool()])` exposes external schema.
- test duplicate tool names raise `ValueError`.

- [ ] **Step 2: Run adapter tests and verify failure**

Run:

```bash
python3 -m unittest tests.test_mcp_adapters tests.test_extended_tools -v
```

Expected: FAIL due missing adapters and duplicate-name behavior.

- [ ] **Step 3: Implement adapters and registry duplicate guard**

In `insightagent/mcp/adapters.py` implement:

- `format_mcp_result(result: dict[str, Any]) -> str`
- `MCPToolAdapter`
- `MCPListResourcesTool`
- `MCPReadResourceTool`
- `MCPListPromptsTool`
- `MCPGetPromptTool`
- `tools_for_client(client, prefix, disabled_tools=())`

In `insightagent/tools/registry.py`:

- Build `_tools` with explicit duplicate detection.
- Raise `ValueError(f"duplicate tool name: {tool.name}")`.
- Keep existing default behavior unchanged.

- [ ] **Step 4: Run adapter tests and commit**

Run:

```bash
python3 -m unittest tests.test_mcp_adapters tests.test_extended_tools -v
```

Expected: PASS.

Commit:

```bash
git add insightagent/mcp/adapters.py insightagent/tools/registry.py tests/test_mcp_adapters.py tests/test_extended_tools.py
git commit -m "Adapt MCP capabilities into tools"
```

## Task 6: MCP Manager

**Files:**
- Create: `insightagent/mcp/manager.py`
- Test: `tests/test_mcp_manager.py`

- [ ] **Step 1: Write failing manager tests**

Create `tests/test_mcp_manager.py`:

- use fake client factory.
- test `start_enabled()` starts enabled servers and skips disabled.
- test failed server does not prevent successful server.
- test `get_tools()` returns adapter tools.
- test duplicate tool names are skipped and reported in status.
- test `restart_server(name)` stops and starts server again.
- test `refresh_server(name)` rebuilds tools.

- [ ] **Step 2: Run manager tests and verify failure**

Run:

```bash
python3 -m unittest tests.test_mcp_manager -v
```

Expected: FAIL because manager does not exist.

- [ ] **Step 3: Implement MCPManager**

Implement:

- `MCPManager(config, client_factory=MCPClient)`.
- `start_enabled(trace=None)`.
- `stop_all(trace=None)`.
- `restart_server(name, trace=None)`.
- `refresh_server(name)`.
- `get_tools()`.
- `status()`.
- duplicate tool detection in manager cache so conflicted tools are skipped, not fatal.

- [ ] **Step 4: Run manager tests and commit**

Run:

```bash
python3 -m unittest tests.test_mcp_manager -v
```

Expected: PASS.

Commit:

```bash
git add insightagent/mcp/manager.py tests/test_mcp_manager.py
git commit -m "Add MCP manager"
```

## Task 7: Streamable HTTP Transport

**Files:**
- Modify: `insightagent/mcp/transports.py`
- Test: `tests/test_mcp_http_transport.py`

- [ ] **Step 1: Write failing HTTP transport tests**

Create `tests/test_mcp_http_transport.py` with local `http.server`:

- server handles POST JSON-RPC and returns JSON initialize response with `Mcp-Session-Id`.
- assert second request sends `MCP-Protocol-Version` and `Mcp-Session-Id`.
- assert configured headers are sent.
- server can return `text/event-stream` body containing `data: <json>`.
- assert 404 after existing session clears session and raises transport error.

- [ ] **Step 2: Run HTTP tests and verify failure**

Run:

```bash
python3 -m unittest tests.test_mcp_http_transport -v
```

Expected: FAIL because `StreamableHttpTransport` is incomplete or missing.

- [ ] **Step 3: Implement StreamableHttpTransport**

Use `urllib.request`:

- POST JSON body.
- headers: `Content-Type`, `Accept`, custom headers.
- after initialize response, caller can call `set_protocol_version(version)`.
- store response header `Mcp-Session-Id`.
- parse JSON response.
- parse SSE by reading lines beginning with `data:`.
- on HTTP 404 with active session, clear session id and raise `MCPTransportError`.

- [ ] **Step 4: Run HTTP tests and commit**

Run:

```bash
python3 -m unittest tests.test_mcp_http_transport -v
```

Expected: PASS.

Commit:

```bash
git add insightagent/mcp/transports.py tests/test_mcp_http_transport.py
git commit -m "Add MCP streamable HTTP transport"
```

## Task 8: Runtime and CLI Integration

**Files:**
- Modify: `insightagent/run_task.py`
- Modify: `insightagent/cli.py`
- Modify: `insightagent/slash_commands.py`
- Modify: `insightagent/trace.py`
- Test: `tests/test_mcp_slash_commands.py`
- Modify Test: `tests/test_slash_commands.py`

- [ ] **Step 1: Write failing slash and trace tests**

Create `tests/test_mcp_slash_commands.py`:

- fake manager with `status`, `get_tools`, `restart_server`, `refresh_server`.
- assert `/mcp status` returns Chinese status lines.
- assert `/mcp tools` lists tool names.
- assert `/mcp restart fake` calls manager and returns success.
- assert `/mcp refresh fake` calls manager and returns success.
- assert manager absent returns `MCP unavailable`.

Modify `tests/test_slash_commands.py`:

- assert `/help` contains `/mcp`.

- [ ] **Step 2: Run slash tests and verify failure**

Run:

```bash
python3 -m unittest tests.test_mcp_slash_commands tests.test_slash_commands -v
```

Expected: FAIL because slash command does not support MCP.

- [ ] **Step 3: Implement slash command and runtime wiring**

Modify `SlashCommandProcessor.__init__`:

```python
def __init__(..., mcp_manager: Any | None = None) -> None:
    self.mcp_manager = mcp_manager
```

Add `/mcp` handling:

- no manager: return `MCP unavailable`
- `status`: render `server status transport tools resources prompts error`
- `tools`: list schema names from manager tools
- `restart <server>`: call manager restart
- `refresh <server>`: call manager refresh

Modify `run_task.py`:

- import `load_mcp_config`, `MCPManager`, `default_tools`.
- build `tool_context`.
- load config and start manager.
- create `ToolRegistry(tools=default_tools(tool_context) + mcp_manager.get_tools(), context=tool_context)`.
- wrap agent run in `try/finally: mcp_manager.stop_all(trace=tracer)`.

Modify `cli.py` similarly, and pass manager to `SlashCommandProcessor`.

Modify `trace.py` to render MCP events with concise sections.

- [ ] **Step 4: Run slash tests and core tests, then commit**

Run:

```bash
python3 -m unittest tests.test_mcp_slash_commands tests.test_slash_commands -v
python3 -m unittest tests.test_agent_loop tests.test_extended_tools -v
```

Expected: PASS.

Commit:

```bash
git add insightagent/run_task.py insightagent/cli.py insightagent/slash_commands.py insightagent/trace.py tests/test_mcp_slash_commands.py tests/test_slash_commands.py
git commit -m "Wire MCP runtime into CLI and runner"
```

## Task 9: Documentation and Examples

**Files:**
- Create: `MCP_GUIDE.md`
- Create: `mcp_config.json.example`
- Modify: `README.md`

- [ ] **Step 1: Add Chinese docs**

Create `MCP_GUIDE.md` with:

- MCP Runtime Layer 是什么。
- 配置加载顺序。
- stdio 示例：Playwright。
- streamable_http 示例。
- `/mcp` 命令说明。
- 安全说明：密钥用环境变量，不提交真实 key。
- 可选 smoke test。

Create `mcp_config.json.example` with valid JSON:

```json
{
  "mcpServers": {
    "playwright": {
      "transport": "stdio",
      "command": "npx",
      "args": ["@playwright/mcp@latest"],
      "enabled": true,
      "startup_timeout": 20,
      "request_timeout": 60,
      "tool_prefix": "mcp_playwright"
    },
    "remote-docs": {
      "transport": "streamable_http",
      "url": "https://example.com/mcp",
      "headers": {
        "Authorization": "Bearer ${REMOTE_MCP_TOKEN}"
      },
      "enabled": false,
      "request_timeout": 60,
      "tool_prefix": "mcp_docs"
    }
  }
}
```

Modify README in Chinese:

- V5 新增能力增加 MCP Runtime Layer。
- 架构列表增加 `mcp/`。
- 新增 MCP 配置和 CLI `/mcp` 小节。

- [ ] **Step 2: Commit docs**

Run:

```bash
python3 -m json.tool mcp_config.json.example >/dev/null
```

Expected: exit code 0.

Commit:

```bash
git add README.md MCP_GUIDE.md mcp_config.json.example
git commit -m "Document MCP runtime layer"
```

## Task 10: Full Verification

**Files:**
- No planned file edits unless verification finds a defect.

- [ ] **Step 1: Run py_compile**

Run:

```bash
python3 -m py_compile $(find insightagent tests -name '*.py' -print)
```

Expected: PASS with no output.

- [ ] **Step 2: Run full unittest suite**

Run:

```bash
python3 -m unittest discover -s tests -v
```

Expected: PASS, including existing 42 tests plus new MCP tests.

- [ ] **Step 3: Inspect git status**

Run:

```bash
git status --short
```

Expected: no uncommitted files.

- [ ] **Step 4: Push if user asks**

Run only after user confirms push:

```bash
git push origin main
```

Expected: remote main receives all MCP commits.
