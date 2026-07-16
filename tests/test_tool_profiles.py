from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from langchain_core.tools import BaseTool

from insightagent.graph.tools import build_builtin_tools
from insightagent.mcp.config import MCPConfig, MCPServerConfig
from insightagent.runtime.tool_context import ToolContext


def load_profile_api():
    try:
        from insightagent.cli.tool_profiles import (
            filter_tools,
            parse_name_list,
            resolve_mcp_server_names,
            select_mcp_config,
            tool_names_for_profile,
        )
    except ModuleNotFoundError:
        raise AssertionError("insightagent.cli.tool_profiles has not been implemented")
    return (
        filter_tools,
        parse_name_list,
        resolve_mcp_server_names,
        select_mcp_config,
        tool_names_for_profile,
    )


class ToolProfileTests(unittest.TestCase):
    def test_coding_basic_profile_keeps_core_tools_without_analysis_tools(self) -> None:
        *_, tool_names_for_profile = load_profile_api()

        names = tool_names_for_profile("coding-basic")

        self.assertIn("execute_command", names)
        self.assertIn("read_file", names)
        self.assertIn("write_file", names)
        self.assertIn("edit_file", names)
        self.assertIn("grep_search", names)
        self.assertIn("todo_write", names)
        self.assertIn("parse_ast", names)
        self.assertIn("get_function_signature", names)
        self.assertNotIn("find_dependencies", names)
        self.assertNotIn("get_code_metrics", names)

    def test_analysis_profile_is_read_only_and_code_analysis_oriented(self) -> None:
        *_, tool_names_for_profile = load_profile_api()

        names = tool_names_for_profile("analysis")

        self.assertIn("read_file", names)
        self.assertIn("grep_search", names)
        self.assertIn("parse_ast", names)
        self.assertIn("get_code_metrics", names)
        self.assertNotIn("execute_command", names)
        self.assertNotIn("write_file", names)
        self.assertNotIn("edit_file", names)

    def test_allowed_tools_can_further_narrow_a_profile(self) -> None:
        filter_tools, parse_name_list, *_ = load_profile_api()
        allowed = parse_name_list(["read_file,grep_search", "git_status"])

        with tempfile.TemporaryDirectory() as directory:
            context = ToolContext(workspace=Path(directory))
            selected = filter_tools(
                build_builtin_tools(context),
                profile="coding-basic",
                allowed_tools=allowed,
            )

        self.assertEqual(
            [tool.name for tool in selected], ["read_file", "grep_search", "git_status"]
        )
        self.assertTrue(all(isinstance(tool, BaseTool) for tool in selected))

    def test_unknown_allowed_tool_is_rejected_instead_of_silently_ignored(self) -> None:
        filter_tools, *_ = load_profile_api()

        with tempfile.TemporaryDirectory() as directory:
            context = ToolContext(workspace=Path(directory))
            with self.assertRaises(ValueError):
                filter_tools(
                    build_builtin_tools(context),
                    profile="coding-basic",
                    allowed_tools={"read_file", "missing"},
                )

    def test_default_runtime_does_not_select_mcp_even_when_config_enabled(self) -> None:
        (
            _filter_tools,
            _parse_name_list,
            resolve_mcp_server_names,
            select_mcp_config,
            _tool_names,
        ) = load_profile_api()
        config = MCPConfig(
            servers={
                "playwright": MCPServerConfig(
                    name="playwright", command="npx", args=["@playwright/mcp"]
                ),
                "github": MCPServerConfig(name="github", command="github-mcp-server"),
            },
            loaded_files=("mcp_config.json",),
        )

        names = resolve_mcp_server_names("coding-basic", None)
        selected = select_mcp_config(config, names)

        self.assertEqual(names, set())
        self.assertEqual(selected.servers, {})
        self.assertEqual(selected.loaded_files, ("mcp_config.json",))

    def test_mcp_selection_requires_profile_or_explicit_server_name(self) -> None:
        (
            _filter_tools,
            _parse_name_list,
            resolve_mcp_server_names,
            select_mcp_config,
            _tool_names,
        ) = load_profile_api()
        config = MCPConfig(
            servers={
                "playwright": MCPServerConfig(
                    name="playwright", command="npx", args=["@playwright/mcp"]
                ),
                "github": MCPServerConfig(name="github", command="github-mcp-server"),
            }
        )

        profile_selected = select_mcp_config(
            config, resolve_mcp_server_names("mcp-playwright", None)
        )
        cli_selected = select_mcp_config(
            config, resolve_mcp_server_names("coding-basic", ["github"])
        )

        self.assertEqual(set(profile_selected.servers), {"playwright"})
        self.assertEqual(set(cli_selected.servers), {"github"})


if __name__ == "__main__":
    unittest.main()
