"""Workspace file tools."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..runtime.tool_context import ToolContext
from .base import read_workspace_text
from .state_tools import javascript_diagnostics, python_diagnostics


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
        self.context.check_path_writable(path)
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
        self.context.check_path_writable(path)
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


@dataclass(frozen=True)
class ApplyEditsTool:
    """Apply several edits across one or more files atomically.

    All edits are validated and staged in memory first; files are only written
    once every edit is known to apply. If any edit fails (missing file, ambiguous
    or absent ``old`` text, oversized result) NO file is changed. After a
    successful write the changed files are run through best-effort diagnostics so
    the model immediately sees whether the multi-file change is syntactically sound.
    """

    context: ToolContext
    name: str = "apply_edits"
    description: str = (
        "Apply multiple edits across one or more files atomically (all-or-nothing), then run "
        "best-effort diagnostics on the changed files. Each edit is either a string replacement "
        "{path, old, new[, replace_all]} or a full write/create {path, content}. Use this for "
        "coordinated multi-file changes. If any edit cannot be applied, no files are changed."
    )
    input_schema: dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "input_schema",
            {
                "type": "object",
                "properties": {
                    "edits": {
                        "type": "array",
                        "description": "Edits to apply atomically, in order.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string", "description": "Workspace-relative path."},
                                "old": {"type": "string", "description": "Exact text to replace."},
                                "new": {"type": "string", "description": "Replacement text."},
                                "content": {"type": "string", "description": "Full file content (write/create)."},
                                "replace_all": {"type": "boolean", "description": "Replace all occurrences of old."},
                            },
                            "required": ["path"],
                            "additionalProperties": False,
                        },
                    }
                },
                "required": ["edits"],
                "additionalProperties": False,
            },
        )

    def run(self, arguments: dict[str, Any]) -> str:
        self.context.check_write_allowed()
        raw_edits = arguments["edits"]
        if not isinstance(raw_edits, list) or not raw_edits:
            raise ValueError("edits must be a non-empty list")
        # Stage every change in memory; nothing is written until all edits validate.
        pending: dict[Path, str] = {}
        counts: list[str] = []
        for index, edit in enumerate(raw_edits, start=1):
            if not isinstance(edit, dict):
                raise ValueError(f"edit #{index} must be an object")
            path = self.context.resolve_workspace_path(str(edit["path"]))
            self.context.check_path_writable(path)
            rel = path.relative_to(self.context.workspace).as_posix()
            has_content = "content" in edit
            has_replacement = "old" in edit or "new" in edit
            if has_content and has_replacement:
                raise ValueError(f"edit #{index} for {rel}: provide either content or old/new, not both")
            if has_content:
                pending[path] = str(edit["content"])
                counts.append(f"{rel}:write")
                continue
            if "old" not in edit or "new" not in edit:
                raise ValueError(f"edit #{index} for {rel}: replacement edits require both old and new")
            if path in pending:
                text = pending[path]
            elif path.is_file():
                text = path.read_text(encoding="utf-8")
            else:
                raise ValueError(f"edit #{index} for {rel}: file not found")
            old = str(edit["old"])
            new = str(edit["new"])
            replace_all = bool(edit.get("replace_all", False))
            occurrences = text.count(old)
            if occurrences == 0:
                raise ValueError(f"edit #{index} for {rel}: old text not found")
            if occurrences > 1 and not replace_all:
                raise ValueError(
                    f"edit #{index} for {rel}: old text appears {occurrences} times; "
                    "set replace_all=true or make old text unique"
                )
            pending[path] = text.replace(old, new) if replace_all else text.replace(old, new, 1)
            counts.append(f"{rel}:{occurrences if replace_all else 1}")
        for path, text in pending.items():
            if len(text) > self.context.max_write_chars:
                rel = path.relative_to(self.context.workspace).as_posix()
                raise ValueError(f"{rel}: edited content too large to write: {len(text)} chars")
        for path, text in pending.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        diagnostics: list[str] = []
        for path in pending:
            if path.suffix == ".py":
                diagnostics.extend(python_diagnostics(self.context, path))
            elif path.suffix == ".js":
                diagnostics.extend(javascript_diagnostics(self.context, path))
        changed = ", ".join(sorted(counts))
        diag_text = "; ".join(diagnostics) if diagnostics else "no diagnostics"
        return f"applied {len(raw_edits)} edit(s) to {len(pending)} file(s) [{changed}] | diagnostics: {diag_text}"
