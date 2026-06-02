"""V3.0 tools with ToolContext, workspace boundaries, and safer edits."""

from __future__ import annotations

import fnmatch
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .tool_context import ToolContext


class Tool(Protocol):
    name: str
    description: str
    input_schema: dict[str, Any]

    def run(self, arguments: dict[str, Any]) -> str:
        ...


@dataclass(frozen=True)
class ExecuteCommandTool:
    context: ToolContext
    name: str = "execute_command"
    description: str = "Run a shell command inside the workspace and return stdout, stderr, and exit code."
    input_schema: dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "input_schema",
            {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to run."},
                    "cwd": {
                        "type": "string",
                        "description": "Optional working directory. Defaults to current process directory.",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Optional timeout in seconds. Defaults to 60.",
                    },
                },
                "required": ["command"],
                "additionalProperties": False,
            },
        )

    def run(self, arguments: dict[str, Any]) -> str:
        command = str(arguments["command"])
        self.context.check_bash_allowed(command)
        cwd = self.context.resolve_workspace_path(str(arguments.get("cwd") or self.context.workspace))
        timeout = int(arguments.get("timeout", 60))
        completed = subprocess.run(
            command,
            shell=True,
            cwd=str(cwd),
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        return (
            f"exit_code: {completed.returncode}\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )


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
        data = path.read_bytes()
        if b"\x00" in data[:4096]:
            raise ValueError(f"refusing to read binary-looking file: {path}")
        text = data.decode("utf-8")
        if len(text) > self.context.max_read_chars:
            raise ValueError(f"file too large to read in V3: {len(text)} chars")
        return text


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


@dataclass(frozen=True)
class GrepSearchTool:
    context: ToolContext
    name: str = "grep_search"
    description: str = "Search workspace text files with a regex pattern and return path, line number, and matching line."
    input_schema: dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "input_schema",
            {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex pattern to search for."},
                    "glob": {"type": "string", "description": "Optional filename glob such as *.py."},
                    "max_results": {"type": "integer", "description": "Maximum matches to return. Defaults to 50."},
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
        )

    def run(self, arguments: dict[str, Any]) -> str:
        pattern = re.compile(str(arguments["pattern"]))
        glob = str(arguments.get("glob") or "*")
        max_results = int(arguments.get("max_results", 50))
        results: list[str] = []
        for path in sorted(self.context.workspace.rglob("*")):
            if len(results) >= max_results:
                break
            if not path.is_file() or not fnmatch.fnmatch(path.name, glob):
                continue
            try:
                data = path.read_bytes()
            except OSError:
                continue
            if b"\x00" in data[:4096]:
                continue
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                continue
            for line_no, line in enumerate(text.splitlines(), start=1):
                if pattern.search(line):
                    rel = path.relative_to(self.context.workspace)
                    results.append(f"{rel}:{line_no}:{line}")
                    if len(results) >= max_results:
                        break
        if not results:
            return "no matches"
        return "\n".join(results)


@dataclass(frozen=True)
class GlobSearchTool:
    context: ToolContext
    name: str = "glob_search"
    description: str = "List workspace files matching a glob pattern."
    input_schema: dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "input_schema",
            {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern such as *.py or **/*.js."},
                    "max_results": {"type": "integer", "description": "Maximum paths to return. Defaults to 100."},
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
        )

    def run(self, arguments: dict[str, Any]) -> str:
        pattern = str(arguments["pattern"])
        max_results = int(arguments.get("max_results", 100))
        results: list[str] = []
        for path in sorted(self.context.workspace.glob(pattern)):
            if len(results) >= max_results:
                break
            if _should_skip_path(path) or not path.is_file():
                continue
            resolved = self.context.resolve_workspace_path(str(path))
            results.append(resolved.relative_to(self.context.workspace).as_posix())
        return "\n".join(results) if results else "no matches"


@dataclass(frozen=True)
class GitStatusTool:
    context: ToolContext
    name: str = "git_status"
    description: str = "Return git status --short for the workspace."
    input_schema: dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "input_schema",
            {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        )

    def run(self, arguments: dict[str, Any]) -> str:
        completed = _run_git(self.context.workspace, ["status", "--short"])
        output = completed.stdout.strip()
        if completed.returncode != 0:
            return f"exit_code: {completed.returncode}\nstderr:\n{completed.stderr.strip()}"
        return output or "clean"


@dataclass(frozen=True)
class GitDiffTool:
    context: ToolContext
    name: str = "git_diff"
    description: str = "Return git diff for the workspace or one workspace path."
    input_schema: dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "input_schema",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Optional workspace-relative path to diff."}
                },
                "required": [],
                "additionalProperties": False,
            },
        )

    def run(self, arguments: dict[str, Any]) -> str:
        command = ["diff", "--"]
        if arguments.get("path"):
            path = self.context.resolve_workspace_path(str(arguments["path"]))
            command.append(path.relative_to(self.context.workspace).as_posix())
        completed = _run_git(self.context.workspace, command)
        if completed.returncode not in {0, 1}:
            return f"exit_code: {completed.returncode}\nstderr:\n{completed.stderr.strip()}"
        return completed.stdout.strip() or "no diff"


@dataclass(frozen=True)
class TodoWriteTool:
    context: ToolContext
    name: str = "todo_write"
    description: str = "Persist the current task todo list inside the workspace."
    input_schema: dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "input_schema",
            {
                "type": "object",
                "properties": {
                    "todos": {
                        "type": "array",
                        "description": "Todo items with content and status.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "content": {"type": "string"},
                                "status": {"type": "string"},
                            },
                            "required": ["content", "status"],
                            "additionalProperties": True,
                        },
                    }
                },
                "required": ["todos"],
                "additionalProperties": False,
            },
        )

    def run(self, arguments: dict[str, Any]) -> str:
        self.context.check_write_allowed()
        raw_todos = arguments["todos"]
        if not isinstance(raw_todos, list):
            raise ValueError("todos must be a list")
        todos = [_normalize_todo(item) for item in raw_todos]
        payload = {"todos": todos}
        content = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        if len(content) > self.context.max_write_chars:
            raise ValueError(f"todo content too large to write: {len(content)} chars")
        path = self.context.resolve_workspace_path(".insightagent/todos.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"todos={len(todos)} path={path.relative_to(self.context.workspace)}"


@dataclass(frozen=True)
class LspDiagnosticsTool:
    context: ToolContext
    name: str = "lsp_diagnostics"
    description: str = "Run best-effort local diagnostics for a workspace file or all supported files."
    input_schema: dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "input_schema",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Optional file or directory to diagnose."},
                    "max_results": {"type": "integer", "description": "Maximum diagnostics to return. Defaults to 100."},
                },
                "required": [],
                "additionalProperties": False,
            },
        )

    def run(self, arguments: dict[str, Any]) -> str:
        root = self.context.resolve_workspace_path(str(arguments.get("path") or "."))
        max_results = int(arguments.get("max_results", 100))
        diagnostics: list[str] = []
        paths = [root] if root.is_file() else sorted(root.rglob("*"))
        for path in paths:
            if len(diagnostics) >= max_results:
                break
            if _should_skip_path(path) or not path.is_file():
                continue
            if path.suffix == ".py":
                diagnostics.extend(_python_diagnostics(self.context, path))
            elif path.suffix == ".js":
                diagnostics.extend(_javascript_diagnostics(self.context, path))
        return "\n".join(diagnostics[:max_results]) if diagnostics else "no diagnostics"


class ToolRegistry:
    def __init__(self, tools: list[Tool] | None = None, context: ToolContext | None = None) -> None:
        self.context = context or ToolContext(workspace=Path.cwd())
        resolved_tools = default_tools(self.context) if tools is None else tools
        self._tools = {tool.name: tool for tool in resolved_tools}

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
    ]


def _run_git(workspace: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(workspace),
        text=True,
        capture_output=True,
        check=False,
    )


def _normalize_todo(item: Any) -> dict[str, str]:
    if not isinstance(item, dict):
        raise ValueError("each todo must be an object")
    content = str(item.get("content", "")).strip()
    status = str(item.get("status", "")).strip()
    if not content:
        raise ValueError("todo content is required")
    if status not in {"pending", "in_progress", "completed"}:
        raise ValueError("todo status must be pending, in_progress, or completed")
    return {"content": content, "status": status}


def _should_skip_path(path: Path) -> bool:
    ignored = {".git", "__pycache__", ".pytest_cache", "node_modules", ".insightagent"}
    return any(part in ignored for part in path.parts)


def _python_diagnostics(context: ToolContext, path: Path) -> list[str]:
    completed = subprocess.run(
        ["python3", "-m", "py_compile", str(path)],
        cwd=str(context.workspace),
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode == 0:
        return []
    rel = path.relative_to(context.workspace).as_posix()
    message = (completed.stderr or completed.stdout).strip().replace("\n", " ")
    return [f"{rel}: SyntaxError: {message}"]


def _javascript_diagnostics(context: ToolContext, path: Path) -> list[str]:
    if shutil.which("node") is None:
        return []
    completed = subprocess.run(
        ["node", "--check", str(path)],
        cwd=str(context.workspace),
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode == 0:
        return []
    rel = path.relative_to(context.workspace).as_posix()
    message = (completed.stderr or completed.stdout).strip().replace("\n", " ")
    return [f"{rel}: JavaScriptError: {message}"]
