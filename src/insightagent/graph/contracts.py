"""SWE-style repository repair contracts for the LangGraph runtime."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping, Sequence


_EXACT_VERIFICATION_PATTERNS = (
    re.compile(
        r"Run this exact verification command before finalizing:\s*([^\r\n]+)",
        re.IGNORECASE,
    ),
    re.compile(r"Verification command:\s*([^\r\n]+)", re.IGNORECASE),
)
_REPOSITORY_REPAIR_MARKERS = (
    "swe-bench",
    "repository repair task",
    "existing repository repair",
)
_INSPECTION_TOOLS = {
    "read_file",
    "grep_search",
    "glob_search",
    "git_status",
    "git_diff",
    "lsp_diagnostics",
    "parse_ast",
    "get_function_signature",
    "find_dependencies",
    "get_code_metrics",
}
_MUTATION_TOOLS = {"write_file", "edit_file"}


class ContractViolation(ValueError):
    """A recoverable task-policy rejection shown to the model."""


@dataclass(frozen=True)
class TaskContract:
    """Pure data policy for a single repository repair turn."""

    task: str = ""
    expected_verification_command: str | None = None
    requires_repository_inspection: bool = False
    protects_test_files: bool = False
    requires_existing_non_test_patch: bool = False
    failing_test_files: tuple[str, ...] = ()

    def validate_before_tool(
        self,
        tool_name: str,
        arguments: Mapping[str, object],
        *,
        inspected_files: Sequence[str],
        changed_files: Sequence[str],
        verification_failed: bool,
        mutates_workspace: bool = False,
        is_mcp: bool = False,
    ) -> None:
        if not self.requires_repository_inspection:
            return

        command = _string_argument(arguments, "command")
        if tool_name == "execute_command":
            if _looks_like_source_inspection(command):
                raise ContractViolation(
                    "Tool contract violation: use read_file, grep_search, or parse_ast for source inspection; "
                    "execute_command is reserved for the declared verification command."
                )
            if not self.expected_verification_command or not command_matches_required(
                command, self.expected_verification_command
            ):
                raise ContractViolation(
                    "Tool contract violation: execute_command must use the exact verification command "
                    "declared by this repository repair task."
                )
        if tool_name == "run_verification" and self.expected_verification_command:
            if not command_matches_required(command, self.expected_verification_command):
                raise ContractViolation(
                    "Tool contract violation: run_verification must use the exact verification command "
                    "declared by this repository repair task."
                )

        is_mutation = tool_name in _MUTATION_TOOLS or mutates_workspace
        if is_mcp and not mutates_workspace:
            raise ContractViolation(
                "Tool contract violation: reject an MCP operation with undeclared workspace side effects "
                "for a repository repair task."
            )
        if is_mutation and not inspected_files:
            raise ContractViolation(
                "Tool contract violation: Inspect the existing repository before modifying files."
            )
        if is_mutation and not _required_tests_are_inspected(
            self.failing_test_files, inspected_files
        ):
            examples = ", ".join(self.failing_test_files[:3])
            raise ContractViolation(
                "Tool contract violation: Read the fail-to-pass test body before modifying source. "
                f"Inspect the relevant test file first: {examples}."
            )
        if verification_failed and is_mutation:
            raise ContractViolation(
                "Tool contract violation: Inspect the latest failing verification before editing again."
            )
        if self.protects_test_files and is_mutation and is_test_file_path(
            _string_argument(arguments, "path")
        ):
            raise ContractViolation(
                "Tool contract violation: Do not modify test files for this repository repair task."
            )
        if _is_verification_tool(tool_name) and self.requires_existing_non_test_patch:
            if not _has_non_test_change(changed_files):
                raise ContractViolation(
                    "Tool contract violation: Modify at least one existing non-test repository file "
                    "before verifying a repository repair."
                )
        if tool_name == "edit_file" and _adds_unrequested_optional_entrypoint(self.task, arguments):
            raise ContractViolation(
                "Tool contract violation: Do not hide required fixes behind new optional flags or "
                "alternate entrypoints."
            )

    def validate_after_tool(
        self,
        tool_name: str,
        arguments: Mapping[str, object],
        result: Mapping[str, object],
        *,
        changed_files: Sequence[str],
        created_files: Sequence[str] = (),
        removed_symbols: Mapping[str, Sequence[str]] | None = None,
        unsafe_paths: Sequence[str] = (),
    ) -> None:
        del tool_name, arguments, result
        if not self.requires_repository_inspection:
            return
        changed = tuple(_normalize_path(path) for path in changed_files)
        created = tuple(_normalize_path(path) for path in created_files)
        if self.protects_test_files and any(is_test_file_path(path) for path in changed + created):
            raise ContractViolation(
                "Tool contract violation: Do not modify test files for this repository repair task."
            )
        if unsafe_paths:
            raise ContractViolation(
                "Tool contract violation: Do not create symbolic links or other non-regular paths "
                "during a repository repair."
            )
        if created and not _has_non_test_change(changed):
            raise ContractViolation(
                "Tool contract violation: Added standalone files are not enough for a repository repair. "
                "Patch an existing non-test implementation file instead."
            )
        for path, symbols in (removed_symbols or {}).items():
            if symbols:
                preview = ", ".join(symbols[:5])
                suffix = "..." if len(symbols) > 5 else ""
                raise ContractViolation(
                    "Tool contract violation: Refusing destructive Python source rewrite that removes "
                    f"existing symbols from {path}: {preview}{suffix}."
                )


def extract_task_contract(task: str) -> TaskContract:
    repository_repair = _requires_repository_inspection(task)
    return TaskContract(
        task=task,
        expected_verification_command=_extract_expected_verification_command(task),
        requires_repository_inspection=repository_repair,
        protects_test_files=repository_repair,
        requires_existing_non_test_patch=repository_repair,
        failing_test_files=_extract_failing_test_files(task),
    )


def command_matches_required(actual: str, expected: str) -> bool:
    return _normalize_command(actual) == _normalize_command(expected)


def is_test_file_path(path: str) -> bool:
    normalized = _normalize_path(path).lower()
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


def _required_tests_are_inspected(
    failing_tests: Sequence[str], inspected_files: Sequence[str]
) -> bool:
    inspected = {_normalize_path(path) for path in inspected_files}
    return all(_normalize_path(path) in inspected for path in failing_tests)


def _has_non_test_change(paths: Sequence[str]) -> bool:
    return any(bool(path) and not is_test_file_path(path) for path in paths)


def _is_verification_tool(tool_name: str) -> bool:
    return tool_name in {"execute_command", "run_verification"}


def _extract_failing_test_files(task: str) -> tuple[str, ...]:
    # Do not infer fail-to-pass tests from the verification command itself:
    # repository-wide commands often mention many unrelated test modules.
    marker = re.search(
        r"(?:fail[- ]to[- ]pass tests?|failing tests?|失败测试|验收测试)\s*:\s*([^\r\n]+)",
        task,
        re.IGNORECASE,
    )
    if marker is None:
        return ()
    files: list[str] = []
    seen: set[str] = set()
    pattern = r"([A-Za-z0-9_./\\-]*test[A-Za-z0-9_./\\-]*\.py)(?:::[A-Za-z0-9_./\\\[\]-]+)*"
    for match in re.finditer(pattern, marker.group(1)):
        path = _normalize_path(match.group(1))
        if path and path not in seen:
            seen.add(path)
            files.append(path)
    return tuple(files)


def _extract_expected_verification_command(task: str) -> str | None:
    for pattern in _EXACT_VERIFICATION_PATTERNS:
        match = pattern.search(task)
        if match:
            command = _trim_verification_command(match.group(1))
            return command or None
    return None


def _trim_verification_command(raw: str) -> str:
    """Extract a command when task prose continues on the same line.

    Task authors frequently write ``command. Then inspect ...`` instead of
    placing the command on its own line. A strict contract must still compare
    the command itself, while preserving dots in paths and Python selectors.
    Inline code spans are preferred; otherwise split only at a sentence dot
    followed by an obvious prose transition.
    """
    value = raw.strip()
    inline = re.match(r"^`([^`]+)`", value)
    if inline:
        return inline.group(1).strip()
    value = re.split(
        r"\.\s+(?=(?:first|then|before|after|please|run|先|然后|随后|再|并|请)\b)",
        value,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    value = re.split(r"。\s*(?=(?:先|然后|随后|再|并|请)\b)", value, maxsplit=1)[0]
    return value.strip().strip("`").strip().rstrip("。")


def _requires_repository_inspection(task: str) -> bool:
    lowered = task.lower()
    return any(marker in lowered for marker in _REPOSITORY_REPAIR_MARKERS)


def _adds_unrequested_optional_entrypoint(task: str, arguments: Mapping[str, object]) -> bool:
    new_text = _string_argument(arguments, "new") or _string_argument(arguments, "content")
    lowered_new = new_text.lower()
    if "@click.option" not in lowered_new and ".add_argument(" not in lowered_new:
        return False
    lowered_task = task.lower()
    requested = (
        " option" in lowered_task
        or " flag" in lowered_task
        or "command-line" in lowered_task
        or "cli option" in lowered_task
        or "新增参数" in task
        or "新增选项" in task
        or re.search(r"--[A-Za-z0-9][A-Za-z0-9_-]*", task) is not None
    )
    return not requested


def _looks_like_source_inspection(command: str) -> bool:
    """Detect shell-based source reads that bypass bounded inspection tools."""
    lowered = command.lower()
    if re.search(r"(^|[;&|]\s*)(?:cat|sed|awk|head|tail)\b", lowered):
        return True
    return bool(
        re.search(r"\bpython(?:3)?\s+-c\b", lowered)
        and re.search(r"(?:open\(|read_text\(|\.read\()", lowered)
    )


def _string_argument(arguments: Mapping[str, object], name: str) -> str:
    value = arguments.get(name)
    return value if isinstance(value, str) else ""


def _normalize_command(command: str) -> str:
    return " ".join(command.strip().split())


def _normalize_path(path: str) -> str:
    return path.replace("\\", "/").strip("/")
