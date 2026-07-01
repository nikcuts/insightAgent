"""Shell execution tools."""

from __future__ import annotations

import importlib.util
import json
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..runtime.tool_context import ToolContext
from .base import should_skip_path


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
        command = normalize_python_command(str(arguments["command"]))
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


# Cap how many Python files go into an auto-detected py_compile command so a
# large workspace cannot produce an unusable, multi-kilobyte shell command.
_MAX_COMPILE_FILES = 100


@dataclass(frozen=True)
class RunVerificationTool:
    """First-class verification: auto-detect and run the project's test/compile
    command, or run an explicit command, and return a structured result.

    The output starts with ``exit_code: <n>`` so the task-state machine and the
    runtime harness can treat it as a verification signal (success vs failure)
    exactly like ``execute_command``.
    """

    context: ToolContext
    name: str = "run_verification"
    description: str = (
        "Verify the project. With no arguments it auto-detects and runs the project's "
        "verification command (npm test, pytest, or python compile). Pass an explicit "
        "`command` to override detection. Returns exit_code, the strategy used, stdout "
        "and stderr. Call this after writing or editing code, and again after every "
        "repair, until exit_code is 0."
    )
    input_schema: dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "input_schema",
            {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Optional explicit verification command. If omitted, it is auto-detected.",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Optional working directory. Defaults to the workspace root.",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Optional timeout in seconds. Defaults to 120.",
                    },
                },
                "required": [],
                "additionalProperties": False,
            },
        )

    def run(self, arguments: dict[str, Any]) -> str:
        cwd = self.context.resolve_workspace_path(str(arguments.get("cwd") or self.context.workspace))
        explicit = arguments.get("command")
        if explicit:
            strategy = "explicit"
            command: str | None = normalize_python_command(str(explicit))
        else:
            strategy, command = detect_verification_command(cwd)
        if command is None:
            return (
                "exit_code: 2\n"
                "strategy: none\n"
                "No verification strategy could be auto-detected (no package.json test "
                "script, no pytest tests, and no Python files were found). Pass an explicit "
                '`command` argument, for example {"command": "python main.py"}.'
            )
        self.context.check_bash_allowed(command)
        timeout = int(arguments.get("timeout", 120))
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
            f"strategy: {strategy}\n"
            f"command: {command}\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )


def detect_verification_command(cwd: Path) -> tuple[str, str | None]:
    """Detect how to verify the project rooted at ``cwd``.

    Returns ``(strategy, command)``. ``command`` is ``None`` when nothing
    suitable was found. Detection is deterministic and never touches the network:

    1. ``package.json`` with a ``test`` script -> ``npm test``
    2. pytest tests present *and* pytest importable -> current Python ``-m pytest -q``
    3. any Python files -> current Python ``-m py_compile <files...>``
    """

    package_json = cwd / "package.json"
    if package_json.is_file():
        try:
            data = json.loads(package_json.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            data = {}
        scripts = data.get("scripts") if isinstance(data, dict) else None
        if isinstance(scripts, dict) and "test" in scripts:
            return "npm-test", "npm test"

    if _has_pytest_targets(cwd) and importlib.util.find_spec("pytest") is not None:
        return "pytest", command_from_args([sys.executable, "-m", "pytest", "-q"])

    py_files = _collect_python_files(cwd)
    if py_files:
        rels = sorted(str(path.relative_to(cwd)) for path in py_files)
        return "py_compile", command_from_args([sys.executable, "-m", "py_compile", *rels])

    return "none", None


def _has_pytest_targets(cwd: Path) -> bool:
    if (cwd / "tests").is_dir():
        return True
    for path in cwd.rglob("*.py"):
        if should_skip_path(path):
            continue
        stem = path.name
        if stem.startswith("test_") or stem.endswith("_test.py"):
            return True
    return False


def _collect_python_files(cwd: Path) -> list[Path]:
    collected: list[Path] = []
    for path in sorted(cwd.rglob("*.py")):
        if should_skip_path(path):
            continue
        collected.append(path)
        if len(collected) >= _MAX_COMPILE_FILES:
            break
    return collected


def command_from_args(args: list[str]) -> str:
    return subprocess.list2cmdline(args)


def normalize_python_command(command: str) -> str:
    if shutil.which("python3") is not None:
        return command
    return re.sub(
        r"(?<![\w./\\-])python3(?![\w./\\-])",
        lambda _match: command_from_args([sys.executable]),
        command,
    )
