"""Tool-surface profiles for the runtime harness."""

from __future__ import annotations

from collections.abc import Iterable

from ..mcp.config import MCPConfig
from ..tools.base import Tool


CODING_BASIC_TOOLS = frozenset(
    {
        "execute_command",
        "run_verification",
        "read_file",
        "write_file",
        "edit_file",
        "grep_search",
        "glob_search",
        "git_status",
        "git_diff",
        "todo_write",
        "lsp_diagnostics",
    }
)
ANALYSIS_TOOLS = frozenset(
    {
        "read_file",
        "grep_search",
        "glob_search",
        "git_status",
        "git_diff",
        "lsp_diagnostics",
        "parse_ast",
        "get_function_signature",
        "find_dependencies",
        "get_code_metrics",
    }
)
CODE_ANALYSIS_TOOLS = frozenset(
    {
        "parse_ast",
        "get_function_signature",
        "find_dependencies",
        "get_code_metrics",
    }
)
ALL_BUILTIN_TOOLS = CODING_BASIC_TOOLS | CODE_ANALYSIS_TOOLS

PROFILE_TOOL_NAMES: dict[str, frozenset[str]] = {
    "coding-basic": CODING_BASIC_TOOLS,
    "analysis": ANALYSIS_TOOLS,
    "all": ALL_BUILTIN_TOOLS,
    "mcp-playwright": CODING_BASIC_TOOLS,
    "mcp-github": CODING_BASIC_TOOLS,
}
PROFILE_MCP_SERVERS: dict[str, frozenset[str]] = {
    "coding-basic": frozenset(),
    "analysis": frozenset(),
    "all": frozenset(),
    "mcp-playwright": frozenset({"playwright"}),
    "mcp-github": frozenset({"github"}),
}
TOOL_PROFILE_CHOICES = tuple(PROFILE_TOOL_NAMES)


def tool_names_for_profile(profile: str) -> set[str]:
    if profile not in PROFILE_TOOL_NAMES:
        raise ValueError(f"unknown tool profile: {profile}")
    return set(PROFILE_TOOL_NAMES[profile])


def parse_name_list(values: Iterable[str] | None) -> set[str] | None:
    if values is None:
        return None
    names: set[str] = set()
    for value in values:
        for part in value.split(","):
            name = part.strip()
            if name:
                names.add(name)
    return names


def filter_tools(tools: list[Tool], profile: str, allowed_tools: set[str] | None = None) -> list[Tool]:
    available_names = {tool.name for tool in tools}
    profile_names = tool_names_for_profile(profile)
    missing_from_runtime = profile_names - available_names
    if missing_from_runtime:
        raise ValueError(f"profile references unavailable tools: {', '.join(sorted(missing_from_runtime))}")
    selected_names = profile_names
    if allowed_tools is not None:
        unknown = allowed_tools - available_names
        if unknown:
            raise ValueError(f"unknown allowed tools: {', '.join(sorted(unknown))}")
        outside_profile = allowed_tools - profile_names
        if outside_profile:
            raise ValueError(f"tools not available in profile {profile}: {', '.join(sorted(outside_profile))}")
        selected_names = allowed_tools
    return [tool for tool in tools if tool.name in selected_names]


def resolve_mcp_server_names(profile: str, explicit_values: Iterable[str] | None) -> set[str]:
    if profile not in PROFILE_MCP_SERVERS:
        raise ValueError(f"unknown tool profile: {profile}")
    names = set(PROFILE_MCP_SERVERS[profile])
    explicit_names = parse_name_list(explicit_values)
    if explicit_names:
        names.update(explicit_names)
    return names


def select_mcp_config(config: MCPConfig, server_names: set[str]) -> MCPConfig:
    if not server_names:
        return MCPConfig(servers={}, loaded_files=config.loaded_files)
    if "all" in server_names:
        return MCPConfig(servers=dict(config.servers), loaded_files=config.loaded_files)
    unknown = server_names - set(config.servers)
    if unknown:
        raise ValueError(f"MCP servers are not configured: {', '.join(sorted(unknown))}")
    return MCPConfig(
        servers={name: config.servers[name] for name in config.servers if name in server_names},
        loaded_files=config.loaded_files,
    )
