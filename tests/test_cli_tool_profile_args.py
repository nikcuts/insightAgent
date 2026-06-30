from __future__ import annotations

import unittest

from insightagent.cli import main as cli, run_task


class ToolProfileArgumentTests(unittest.TestCase):
    def test_run_task_defaults_to_coding_basic_without_mcp(self) -> None:
        args = run_task.build_parser().parse_args([])

        self.assertEqual(args.tool_profile, "coding-basic")
        self.assertIsNone(args.allowed_tools)
        self.assertIsNone(args.enable_mcp_server)

    def test_run_task_accepts_allowed_tools_and_explicit_mcp_servers(self) -> None:
        args = run_task.build_parser().parse_args(
            [
                "--tool-profile",
                "analysis",
                "--allowed-tools",
                "read_file,grep_search",
                "--allowed-tools",
                "git_status",
                "--enable-mcp-server",
                "github",
            ]
        )

        self.assertEqual(args.tool_profile, "analysis")
        self.assertEqual(args.allowed_tools, ["read_file,grep_search", "git_status"])
        self.assertEqual(args.enable_mcp_server, ["github"])

    def test_interactive_cli_exposes_same_tool_surface_options(self) -> None:
        args = cli.build_parser().parse_args(
            [
                "--tool-profile",
                "mcp-playwright",
                "--allowed-tools",
                "read_file,grep_search",
                "--enable-mcp-server",
                "github,playwright",
            ]
        )

        self.assertEqual(args.tool_profile, "mcp-playwright")
        self.assertEqual(args.allowed_tools, ["read_file,grep_search"])
        self.assertEqual(args.enable_mcp_server, ["github,playwright"])


if __name__ == "__main__":
    unittest.main()
