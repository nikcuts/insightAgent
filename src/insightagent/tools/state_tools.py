"""Repository state, todo, and diagnostics tools."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..runtime.tool_context import ToolContext
from .base import should_skip_path


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
        completed = run_git(self.context.workspace, ["status", "--short"])
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
        completed = run_git(self.context.workspace, command)
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
        todos = [normalize_todo(item) for item in raw_todos]
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
            if should_skip_path(path) or not path.is_file():
                continue
            if path.suffix == ".py":
                diagnostics.extend(python_diagnostics(self.context, path))
            elif path.suffix == ".js":
                diagnostics.extend(javascript_diagnostics(self.context, path))
        return "\n".join(diagnostics[:max_results]) if diagnostics else "no diagnostics"


def run_git(workspace: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(workspace),
        text=True,
        capture_output=True,
        check=False,
    )


def normalize_todo(item: Any) -> dict[str, str]:
    if not isinstance(item, dict):
        raise ValueError("each todo must be an object")
    content = str(item.get("content", "")).strip()
    status = str(item.get("status", "")).strip()
    if not content:
        raise ValueError("todo content is required")
    if status not in {"pending", "in_progress", "completed"}:
        raise ValueError("todo status must be pending, in_progress, or completed")
    return {"content": content, "status": status}


def python_diagnostics(context: ToolContext, path: Path) -> list[str]:
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


def javascript_diagnostics(context: ToolContext, path: Path) -> list[str]:
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
