"""Shared tool contracts and helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from ..runtime.tool_context import ToolContext


class Tool(Protocol):
    name: str
    description: str
    input_schema: dict[str, Any]

    def run(self, arguments: dict[str, Any]) -> str:
        ...


def read_workspace_text(context: ToolContext, path: Path) -> str:
    data = path.read_bytes()
    if b"\x00" in data[:4096]:
        raise ValueError(f"refusing to read binary-looking file: {path}")
    text = data.decode("utf-8")
    if len(text) > context.max_read_chars:
        raise ValueError(f"file too large to read: {len(text)} chars")
    return text


def should_skip_path(path: Path) -> bool:
    # Keep repository search aligned with snapshot hygiene. Virtualenvs and
    # build products are implementation dependencies, not agent evidence.
    ignored = {
        ".git",
        ".insightagent",
        ".hg",
        ".svn",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "venv",
        "node_modules",
        "dist",
        "build",
    }
    return any(part in ignored for part in path.parts)
