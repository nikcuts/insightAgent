"""Repository structure snapshots for repair-oriented agent turns."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .task_contracts import is_test_file_path


IGNORED_DIRECTORIES = {
    ".git",
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

IGNORED_FILE_NAMES = {
    ".env",
    ".env.local",
    ".env.production",
    ".env.development",
    ".env.test",
}

IGNORED_FILE_SUFFIXES = (
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".crt",
)


@dataclass(frozen=True)
class RepositorySnapshot:
    content: str
    file_count: int
    omitted_count: int


def build_repository_snapshot(workspace: Path, max_files: int = 80) -> RepositorySnapshot:
    root = workspace.expanduser().resolve()
    files = _list_repository_files(root, max_files=max_files)
    omitted_count = max(0, _count_repository_files(root, limit=max_files + 1) - len(files))
    lines = [
        "Repository snapshot for repair (structure only; no file contents):",
        f"Workspace: {root.name}",
        "Files:",
    ]
    if files:
        lines.extend(f"- {_format_file(path)}" for path in files)
    else:
        lines.append("- <no files found>")
    if omitted_count:
        lines.append(f"- ... {omitted_count} more files omitted")
    lines.extend(
        [
            "",
            "Use read_file, grep_search, glob_search, git_status, or git_diff before modifying files.",
            "Do not infer source contents from this snapshot; it is only a repository map.",
        ]
    )
    return RepositorySnapshot(
        content="\n".join(lines),
        file_count=len(files),
        omitted_count=omitted_count,
    )


def _list_repository_files(root: Path, max_files: int) -> list[Path]:
    results: list[Path] = []
    for directory, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name not in IGNORED_DIRECTORIES)
        for filename in sorted(filenames):
            if _should_ignore_file(filename):
                continue
            path = Path(directory) / filename
            try:
                relative = path.relative_to(root)
            except ValueError:
                continue
            results.append(relative)
            if len(results) >= max_files:
                return results
    return results


def _count_repository_files(root: Path, limit: int) -> int:
    count = 0
    for directory, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in IGNORED_DIRECTORIES]
        for filename in filenames:
            if _should_ignore_file(filename):
                continue
            count += 1
            if count >= limit:
                return count
    return count


def _format_file(path: Path) -> str:
    text = path.as_posix()
    if is_test_file_path(text):
        return f"{text} [test]"
    return text


def _should_ignore_file(filename: str) -> bool:
    lowered = filename.lower()
    return lowered in IGNORED_FILE_NAMES or lowered.endswith(IGNORED_FILE_SUFFIXES)
