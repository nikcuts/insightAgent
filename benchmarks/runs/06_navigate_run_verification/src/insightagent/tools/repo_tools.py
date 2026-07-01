"""Repository-level retrieval tools.

These give the agent a cheap, deterministic way to understand an unfamiliar
codebase before editing it: ``repo_map`` summarizes structure and per-file
symbols, and ``find_symbol`` locates where a class/function/method is defined.
Both are pure-Python (stdlib ``ast``) and read-only.
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from ..runtime.tool_context import ToolContext
from .base import read_workspace_text, should_skip_path


_MAX_FILES = 300
_MAX_OTHER_FILES = 200


@dataclass(frozen=True)
class RepoMapTool:
    context: ToolContext
    name: str = "repo_map"
    description: str = (
        "Map the repository: list files and, for each Python file, its top-level classes "
        "(with method names) and functions. Use this first to understand an unfamiliar "
        "codebase and decide which files to open, instead of blindly grepping."
    )
    input_schema: dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "input_schema",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Optional subdirectory to map. Defaults to the workspace root."},
                    "max_files": {"type": "integer", "description": "Maximum Python files to detail. Defaults to 300."},
                },
                "required": [],
                "additionalProperties": False,
            },
        )

    def run(self, arguments: dict[str, Any]) -> str:
        root = self.context.resolve_workspace_path(str(arguments.get("path") or "."))
        max_files = int(arguments.get("max_files", _MAX_FILES))
        files: list[dict[str, Any]] = []
        other_files: list[str] = []
        truncated = False
        for path in _iter_files(root):
            rel = path.relative_to(self.context.workspace).as_posix()
            if path.suffix == ".py":
                if len(files) >= max_files:
                    truncated = True
                    continue
                files.append(self._summarize_python(path, rel))
            elif len(other_files) < _MAX_OTHER_FILES:
                other_files.append(rel)
        payload: dict[str, Any] = {
            "root": root.relative_to(self.context.workspace).as_posix() or ".",
            "python_files": len(files),
            "other_files_count": len(other_files),
            "files": files,
            "other_files": other_files,
        }
        if truncated:
            payload["truncated"] = True
        return json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True)

    def _summarize_python(self, path: Path, rel: str) -> dict[str, Any]:
        tree = _safe_parse(self.context, path)
        if tree is None:
            return {"path": rel, "error": "could not parse"}
        classes = []
        functions = []
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                methods = [
                    child.name
                    for child in node.body
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                ]
                classes.append({"name": node.name, "line": node.lineno, "methods": methods})
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions.append({"name": node.name, "line": node.lineno})
        return {"path": rel, "classes": classes, "functions": functions}


@dataclass(frozen=True)
class FindSymbolTool:
    context: ToolContext
    name: str = "find_symbol"
    description: str = (
        "Find where a class, function, or method is defined across the repository. Returns the "
        "matching file:line locations. Use this to jump straight to a definition instead of grepping."
    )
    input_schema: dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "input_schema",
            {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Symbol name to locate."},
                    "kind": {
                        "type": "string",
                        "description": "Optional filter: class, function, or method.",
                    },
                    "path": {"type": "string", "description": "Optional subdirectory to search. Defaults to the workspace root."},
                },
                "required": ["name"],
                "additionalProperties": False,
            },
        )

    def run(self, arguments: dict[str, Any]) -> str:
        name = str(arguments["name"])
        kind_filter = str(arguments.get("kind") or "").strip().lower() or None
        if kind_filter is not None and kind_filter not in {"class", "function", "method"}:
            raise ValueError("kind must be one of: class, function, method")
        root = self.context.resolve_workspace_path(str(arguments.get("path") or "."))
        matches: list[dict[str, Any]] = []
        for path in _iter_files(root):
            if path.suffix != ".py":
                continue
            tree = _safe_parse(self.context, path)
            if tree is None:
                continue
            rel = path.relative_to(self.context.workspace).as_posix()
            matches.extend(_match_symbols(tree, rel, name, kind_filter))
        matches.sort(key=lambda item: (item["path"], item["line"]))
        return json.dumps({"name": name, "matches": matches}, indent=2, ensure_ascii=False, sort_keys=True)


def _match_symbols(tree: ast.Module, rel: str, name: str, kind_filter: str | None) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            if node.name == name and kind_filter in (None, "class"):
                found.append({"path": rel, "line": node.lineno, "kind": "class", "name": name})
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == name:
                    if kind_filter in (None, "method"):
                        found.append(
                            {
                                "path": rel,
                                "line": child.lineno,
                                "kind": "method",
                                "name": name,
                                "class": node.name,
                            }
                        )
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == name and kind_filter in (None, "function"):
                found.append({"path": rel, "line": node.lineno, "kind": "function", "name": name})
    return found


def _iter_files(root: Path) -> Iterator[Path]:
    if root.is_file():
        yield root
        return
    for path in sorted(root.rglob("*")):
        if should_skip_path(path) or not path.is_file():
            continue
        yield path


def _safe_parse(context: ToolContext, path: Path) -> ast.Module | None:
    try:
        source = read_workspace_text(context, path)
        return ast.parse(source)
    except (SyntaxError, ValueError, OSError, UnicodeDecodeError):
        return None
