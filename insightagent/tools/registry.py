"""Tool registry and default tool construction."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..tool_context import ToolContext
from .base import Tool
from .code_analysis_tools import (
    FindDependenciesTool,
    GetCodeMetricsTool,
    GetFunctionSignatureTool,
    ParseAstTool,
)
from .execution_tools import ExecuteCommandTool
from .file_tools import EditFileTool, ReadFileTool, WriteFileTool
from .search_tools import GrepSearchTool, GlobSearchTool
from .state_tools import GitDiffTool, GitStatusTool, LspDiagnosticsTool, TodoWriteTool


class ToolRegistry:
    def __init__(self, tools: list[Tool] | None = None, context: ToolContext | None = None) -> None:
        self.context = context or ToolContext(workspace=Path.cwd())
        resolved_tools = default_tools(self.context) if tools is None else tools
        self._tools: dict[str, Tool] = {}
        for tool in resolved_tools:
            if tool.name in self._tools:
                raise ValueError(f"duplicate tool name: {tool.name}")
            self._tools[tool.name] = tool

    def schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema,
            }
            for tool in self._tools.values()
        ]

    def run(self, name: str, arguments: dict[str, Any]) -> str:
        if name not in self._tools:
            raise KeyError(f"unknown tool: {name}")
        return self._tools[name].run(arguments)


def default_tools(context: ToolContext | None = None) -> list[Tool]:
    resolved_context = context or ToolContext(workspace=Path.cwd())
    return [
        ExecuteCommandTool(resolved_context),
        ReadFileTool(resolved_context),
        WriteFileTool(resolved_context),
        EditFileTool(resolved_context),
        GrepSearchTool(resolved_context),
        GlobSearchTool(resolved_context),
        GitStatusTool(resolved_context),
        GitDiffTool(resolved_context),
        TodoWriteTool(resolved_context),
        LspDiagnosticsTool(resolved_context),
        ParseAstTool(resolved_context),
        GetFunctionSignatureTool(resolved_context),
        FindDependenciesTool(resolved_context),
        GetCodeMetricsTool(resolved_context),
    ]
