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
    description: str = (
        "Diagnose a workspace file or directory. Uses pyright for real type/semantic diagnostics "
        "when it is installed, otherwise falls back to a best-effort syntax check (py_compile / "
        "node --check). Set checker to pyright or basic to force a backend."
    )
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
                    "checker": {
                        "type": "string",
                        "description": "Diagnostic backend: auto (default), pyright, or basic.",
                    },
                },
                "required": [],
                "additionalProperties": False,
            },
        )

    def run(self, arguments: dict[str, Any]) -> str:
        root = self.context.resolve_workspace_path(str(arguments.get("path") or "."))
        max_results = int(arguments.get("max_results", 100))
        checker = str(arguments.get("checker") or "auto").strip().lower()
        if checker not in {"auto", "pyright", "basic"}:
            raise ValueError("checker must be one of: auto, pyright, basic")
        use_pyright = checker == "pyright" or (checker == "auto" and pyright_available())
        if use_pyright:
            diagnostics = pyright_diagnostics(self.context, root, max_results)
            if diagnostics is None:
                if checker == "pyright":
                    return "pyright is not available; install pyright or use checker=basic"
                diagnostics = basic_diagnostics(self.context, root, max_results)
        else:
            diagnostics = basic_diagnostics(self.context, root, max_results)
        return "\n".join(diagnostics[:max_results]) if diagnostics else "no diagnostics"


def basic_diagnostics(context: ToolContext, root: Path, max_results: int) -> list[str]:
    """Best-effort syntax diagnostics without an external language server."""

    diagnostics: list[str] = []
    paths = [root] if root.is_file() else sorted(root.rglob("*"))
    for path in paths:
        if len(diagnostics) >= max_results:
            break
        if should_skip_path(path) or not path.is_file():
            continue
        if path.suffix == ".py":
            diagnostics.extend(python_diagnostics(context, path))
        elif path.suffix == ".js":
            diagnostics.extend(javascript_diagnostics(context, path))
    return diagnostics


def pyright_available() -> bool:
    return shutil.which("pyright") is not None


def pyright_diagnostics(context: ToolContext, root: Path, max_results: int) -> list[str] | None:
    """Run pyright over ``root`` and return formatted diagnostics.

    Returns ``None`` when pyright is not installed so callers can fall back. An
    empty list means pyright ran cleanly with no diagnostics.
    """

    if not pyright_available():
        return None
    completed = subprocess.run(
        ["pyright", "--outputjson", str(root)],
        cwd=str(context.workspace),
        text=True,
        capture_output=True,
        check=False,
    )
    return _parse_pyright_output(completed.stdout, context, max_results)


def _parse_pyright_output(stdout: str, context: ToolContext, max_results: int) -> list[str]:
    try:
        data = json.loads(stdout)
    except (ValueError, TypeError):
        return []
    raw = data.get("generalDiagnostics") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []
    results: list[str] = []
    for diag in raw:
        if len(results) >= max_results:
            break
        if not isinstance(diag, dict):
            continue
        file_path = str(diag.get("file", ""))
        try:
            rel = Path(file_path).resolve().relative_to(context.workspace).as_posix()
        except (ValueError, OSError):
            rel = file_path
        severity = str(diag.get("severity", "error"))
        message = str(diag.get("message", "")).replace("\n", " ")
        rule = diag.get("rule")
        start = (diag.get("range") or {}).get("start") if isinstance(diag.get("range"), dict) else None
        line = (start or {}).get("line", 0) + 1
        column = (start or {}).get("character", 0) + 1
        rule_suffix = f" ({rule})" if rule else ""
        results.append(f"{rel}:{line}:{column}: {severity}: {message}{rule_suffix}")
    return results


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
