"""Workspace search tools."""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from typing import Any

from ..runtime.tool_context import ToolContext
from .base import should_skip_path


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
            if not path.is_file():
                continue
            rel = path.relative_to(self.context.workspace).as_posix()
            if not _matches_grep_glob(path.name, rel, glob):
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
                    results.append(f"{rel}:{line_no}:{line}")
                    if len(results) >= max_results:
                        break
        return "\n".join(results) if results else "no matches"


def _matches_grep_glob(file_name: str, relative_path: str, pattern: str) -> bool:
    return any(
        fnmatch.fnmatch(file_name, variant) or fnmatch.fnmatch(relative_path, variant)
        for variant in _grep_glob_variants(pattern)
    )


def _grep_glob_variants(pattern: str) -> list[str]:
    normalized = pattern.replace("\\", "/")
    variants = [normalized]
    if not normalized.startswith("**/"):
        variants.append(f"**/{normalized}")
    if "/**/" in normalized:
        collapsed = normalized.replace("/**/", "/")
        variants.append(collapsed)
        if not collapsed.startswith("**/"):
            variants.append(f"**/{collapsed}")
    return list(dict.fromkeys(variants))


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
        for path in sorted(self.context.workspace.rglob("*")):
            if len(results) >= max_results:
                break
            if should_skip_path(path) or not path.is_file():
                continue
            relative_path = path.relative_to(self.context.workspace).as_posix()
            if not _matches_grep_glob(path.name, relative_path, pattern):
                continue
            results.append(relative_path)
        return "\n".join(results) if results else "no matches"
