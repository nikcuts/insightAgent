"""Shell execution tools."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..runtime.tool_context import SandboxUnavailable, ToolContext
from .base import should_skip_path


_PROCESS_TERMINATION_GRACE_SECONDS = 0.2


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
                    "command": {
                        "type": "string",
                        "description": "Shell command to run.",
                    },
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
        return self._run(arguments, turn_timeout=None)

    def run_with_turn_timeout(
        self, arguments: dict[str, Any], turn_timeout: float | None
    ) -> str:
        return self._run(arguments, turn_timeout=turn_timeout)

    def _run(self, arguments: dict[str, Any], turn_timeout: float | None) -> str:
        command = normalize_python_command(str(arguments["command"]))
        if self.context.execution_mode == "host":
            command = normalize_host_workspace_alias(command, self.context.workspace)
        self.context.check_bash_allowed(command)
        cwd = self.context.resolve_workspace_path(
            str(arguments.get("cwd") or self.context.workspace)
        )
        timeout = int(arguments.get("timeout", 60))
        effective_timeout = _effective_timeout(timeout, turn_timeout)
        completed = (
            run_sandbox_command(command, cwd, effective_timeout, self.context)
            if self.context.execution_mode == "sandbox"
            else run_shell_command(command, cwd, effective_timeout)
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
        return self._run(arguments, turn_timeout=None)

    def run_with_turn_timeout(
        self, arguments: dict[str, Any], turn_timeout: float | None
    ) -> str:
        return self._run(arguments, turn_timeout=turn_timeout)

    def _run(self, arguments: dict[str, Any], turn_timeout: float | None) -> str:
        cwd = self.context.resolve_workspace_path(
            str(arguments.get("cwd") or self.context.workspace)
        )
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
        effective_timeout = _effective_timeout(timeout, turn_timeout)
        completed = (
            run_sandbox_command(command, cwd, effective_timeout, self.context)
            if self.context.execution_mode == "sandbox"
            else run_shell_command(command, cwd, effective_timeout)
        )
        return (
            f"exit_code: {completed.returncode}\n"
            f"strategy: {strategy}\n"
            f"command: {command}\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )


def run_shell_command(
    command: str,
    cwd: Path,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    """Run a shell command and reclaim its descendants on a timeout."""
    process = subprocess.Popen(
        command,
        shell=True,
        cwd=str(cwd),
        env=_workspace_environment(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _terminate_process_group(process)
        stdout, stderr = process.communicate()
        raise subprocess.TimeoutExpired(
            command, timeout, output=stdout, stderr=stderr
        ) from None
    return subprocess.CompletedProcess(
        args=command,
        returncode=process.returncode,
        stdout=stdout,
        stderr=stderr,
    )


def run_sandbox_command(
    command: str,
    cwd: Path,
    timeout: float,
    context: ToolContext,
) -> subprocess.CompletedProcess[str]:
    """Run a command in a disposable Docker container with no network access.

    This is deliberately fail-closed: a missing Docker runtime raises instead
    of silently executing the command with host privileges.
    """
    docker = shutil.which("docker")
    if docker is None:
        raise SandboxUnavailable(
            "sandbox mode requires the Docker CLI; host execution was not attempted"
        )
    try:
        relative_cwd = cwd.relative_to(context.workspace)
    except ValueError as error:
        raise SandboxUnavailable("sandbox cwd must be inside the workspace") from error
    container_cwd = "/workspace"
    if str(relative_cwd) != ".":
        container_cwd += "/" + str(relative_cwd)
    argv = [
        docker,
        "run",
        "--rm",
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        f"--pids-limit={context.sandbox_pids_limit}",
        f"--memory={context.sandbox_memory_mb}m",
        f"--cpus={context.sandbox_cpus:g}",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=64m",
        "--volume",
        f"{context.workspace}:/workspace:rw",
        "--workdir",
        container_cwd,
        context.sandbox_image,
        "/bin/sh",
        "-lc",
        command,
    ]
    environment = {"PATH": os.environ.get("PATH", "")}
    process = subprocess.Popen(
        argv,
        cwd=str(context.workspace),
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _terminate_process_group(process)
        stdout, stderr = process.communicate()
        raise subprocess.TimeoutExpired(argv, timeout, output=stdout, stderr=stderr) from None
    return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)


def _workspace_environment(cwd: Path) -> dict[str, str]:
    """Prefer the current checkout's ``src`` tree over installed packages."""
    environment = os.environ.copy()
    src_dir = cwd / "src"
    if not src_dir.is_dir():
        return environment
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(src_dir) if not existing else f"{src_dir}{os.pathsep}{existing}"
    )
    return environment


def _terminate_process_group(
    process: subprocess.Popen[str],
    *,
    grace_seconds: float = _PROCESS_TERMINATION_GRACE_SECONDS,
) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    if grace_seconds:
        try:
            process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _effective_timeout(requested: int, turn_timeout: float | None) -> float:
    if turn_timeout is None:
        return float(requested)
    return min(float(requested), max(0.001, turn_timeout))


def normalize_host_workspace_alias(command: str, workspace: Path) -> str:
    """Translate common container workspace paths when running on the host.

    Models sometimes retain ``/workspace`` from a container-oriented prompt. In
    host mode that path is usually absent, so a deterministic alias keeps the
    command inside the already-approved workspace instead of producing a
    misleading environment failure. Sandbox commands intentionally keep the
    container path unchanged.
    """
    if workspace == Path("/workspace") or Path("/workspace").exists():
        return command
    replacement = shlex.quote(str(workspace))
    return re.sub(r"(?<![A-Za-z0-9_:/.-])/workspace(?=(?:/|\s|$))", replacement, command)


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
        return "py_compile", command_from_args(
            [sys.executable, "-m", "py_compile", *rels]
        )

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
