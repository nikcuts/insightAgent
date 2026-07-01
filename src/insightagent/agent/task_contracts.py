"""Task-level runtime contracts for repository repair turns."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class TaskContract:
    expected_verification_command: str | None = None
    requires_repository_inspection: bool = False
    protects_test_files: bool = False
    requires_existing_non_test_patch: bool = False
    failing_test_files: tuple[str, ...] = ()


EXACT_VERIFICATION_PATTERNS = (
    re.compile(r"Run this exact verification command before finalizing:\s*([^\r\n]+)", re.IGNORECASE),
    re.compile(r"Verification command:\s*([^\r\n]+)", re.IGNORECASE),
)

REPOSITORY_REPAIR_MARKERS = (
    "swe-bench",
    "repository repair task",
    "existing repository repair",
)


def extract_task_contract(task: str) -> TaskContract:
    repository_repair = _requires_repository_inspection(task)
    return TaskContract(
        expected_verification_command=_extract_expected_verification_command(task),
        requires_repository_inspection=repository_repair,
        protects_test_files=repository_repair,
        requires_existing_non_test_patch=repository_repair,
        failing_test_files=_extract_failing_test_files(task),
    )


def command_matches_required(actual: str, expected: str) -> bool:
    return _normalize_command(actual) == _normalize_command(expected)


def is_test_file_path(path: str) -> bool:
    normalized = path.replace("\\", "/").lower().strip("/")
    filename = normalized.rsplit("/", 1)[-1]
    return (
        normalized.startswith("tests/")
        or "/tests/" in normalized
        or filename.startswith("test_")
        or filename.endswith("_test.py")
        or filename.endswith(".test.js")
        or filename.endswith(".test.ts")
        or filename.endswith(".spec.js")
        or filename.endswith(".spec.ts")
    )


def _extract_failing_test_files(task: str) -> tuple[str, ...]:
    files: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r"([A-Za-z0-9_./\\-]*test[A-Za-z0-9_./\\-]*\.py)(?:::[A-Za-z0-9_./\\\[\]-]+)*", task):
        path = match.group(1).replace("\\", "/").strip("/")
        if path and path not in seen:
            seen.add(path)
            files.append(path)
    return tuple(files)


def _extract_expected_verification_command(task: str) -> str | None:
    for pattern in EXACT_VERIFICATION_PATTERNS:
        match = pattern.search(task)
        if match:
            command = match.group(1).strip()
            return command.strip("`").strip() or None
    return None


def _requires_repository_inspection(task: str) -> bool:
    lowered = task.lower()
    return any(marker in lowered for marker in REPOSITORY_REPAIR_MARKERS)


def _normalize_command(command: str) -> str:
    return " ".join(command.strip().split())
