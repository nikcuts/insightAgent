"""Workspace file tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..runtime.tool_context import ToolContext
from .base import read_workspace_text


@dataclass(frozen=True)
class ReadFileTool:
    context: ToolContext
    name: str = "read_file"
    description: str = "Read a UTF-8 text file inside the workspace."
    input_schema: dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "input_schema",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the text file to read."}
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        )

    def run(self, arguments: dict[str, Any]) -> str:
        path = self.context.resolve_workspace_path(str(arguments["path"]))
        return read_workspace_text(self.context, path)


@dataclass(frozen=True)
class WriteFileTool:
    context: ToolContext
    name: str = "write_file"
    description: str = "Overwrite a UTF-8 text file inside the workspace. Prefer edit_file for local changes."
    input_schema: dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "input_schema",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to write."},
                    "content": {
                        "type": "string",
                        "description": "Full file content. Existing file content is replaced.",
                    },
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        )

    def run(self, arguments: dict[str, Any]) -> str:
        self.context.check_write_allowed()
        path = self.context.resolve_workspace_path(str(arguments["path"]))
        path.parent.mkdir(parents=True, exist_ok=True)
        content = str(arguments["content"])
        if len(content) > self.context.max_write_chars:
            raise ValueError(f"content too large to write in V3: {len(content)} chars")
        path.write_text(content, encoding="utf-8")
        return f"wrote {len(content)} bytes to {path}"


@dataclass(frozen=True)
class EditFileTool:
    context: ToolContext
    name: str = "edit_file"
    description: str = "Apply a local string replacement to a UTF-8 text file inside the workspace."
    input_schema: dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "input_schema",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to edit."},
                    "old": {"type": "string", "description": "Exact text to replace."},
                    "new": {"type": "string", "description": "Replacement text."},
                    "replace_all": {"type": "boolean", "description": "Replace all occurrences. Defaults to false."},
                },
                "required": ["path", "old", "new"],
                "additionalProperties": False,
            },
        )

    def run(self, arguments: dict[str, Any]) -> str:
        self.context.check_write_allowed()
        path = self.context.resolve_workspace_path(str(arguments["path"]))
        old = str(arguments["old"])
        new = str(arguments["new"])
        replace_all = bool(arguments.get("replace_all", False))
        text = path.read_text(encoding="utf-8")
        count = text.count(old)
        if count == 0:
            raise ValueError("old text not found")
        if count > 1 and not replace_all:
            raise ValueError(f"old text appears {count} times; set replace_all=true or make old text unique")
        updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
        if len(updated) > self.context.max_write_chars:
            raise ValueError(f"edited content too large to write in V3: {len(updated)} chars")
        path.write_text(updated, encoding="utf-8")
        return f"edited {path}; replacements={count if replace_all else 1}"
