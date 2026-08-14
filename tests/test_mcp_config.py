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

            config = load_mcp_config(
                workspace,
                user_config_home=home / ".insightagent",
                allow_workspace_config=True,
            )

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

    def test_loads_start_directory_config_before_workspace_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "demo"
            workspace.mkdir()
            (root / "mcp_config.json").write_text(
                json.dumps({"mcpServers": {"playwright": {"command": "npx", "args": ["root"]}}}),
                encoding="utf-8",
            )
            (workspace / "mcp_config.json").write_text(
                json.dumps({"mcpServers": {"playwright": {"args": ["workspace"]}}}),
                encoding="utf-8",
            )

            config = load_mcp_config(
                workspace,
                user_config_home=root / "no-home",
                start_dir=root,
                allow_workspace_config=True,
            )

        self.assertEqual(config.servers["playwright"].command, "npx")
        self.assertEqual(config.servers["playwright"].args, ["workspace"])
        self.assertEqual(len(config.loaded_files), 2)

    def test_workspace_mcp_config_is_ignored_without_explicit_trust(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "mcp_config.json").write_text(
                json.dumps({"mcpServers": {"untrusted": {"command": "dangerous-tool"}}}),
                encoding="utf-8",
            )

            config = load_mcp_config(
                workspace,
                user_config_home=root / "home",
                start_dir=workspace,
            )

        self.assertEqual(config.servers, {})
        self.assertEqual(config.loaded_files, ())


if __name__ == "__main__":
    unittest.main()
