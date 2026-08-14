"""Tool context, workspace boundaries, and permission checks for V3.0."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path


class PermissionDenied(RuntimeError):
    """Raised when a tool violates the current permission policy."""


class WorkspaceViolation(RuntimeError):
    """Raised when a tool attempts to access a path outside the workspace."""


class SandboxUnavailable(RuntimeError):
    """Raised when sandbox mode is enabled but its runtime is unavailable."""


@dataclass(frozen=True)
class ToolContext:
    workspace: Path
    permission_mode: str = "workspace-write"
    approval_mode: str = "deny"
    execution_mode: str = "host"
    sandbox_image: str = "python:3.11-slim"
    sandbox_memory_mb: int = 512
    sandbox_cpus: float = 1.0
    sandbox_pids_limit: int = 128
    require_confirmation: bool = True
    max_read_chars: int = 500_000
    max_write_chars: int = 500_000

    def __post_init__(self) -> None:
        object.__setattr__(self, "workspace", self.workspace.expanduser().resolve())
        if self.permission_mode not in {"read-only", "workspace-write"}:
            raise ValueError("permission_mode must be read-only or workspace-write")
        if self.approval_mode not in {"deny", "interrupt"}:
            raise ValueError("approval_mode must be deny or interrupt")
        if self.execution_mode not in {"host", "sandbox"}:
            raise ValueError("execution_mode must be host or sandbox")
        if not self.sandbox_image.strip():
            raise ValueError("sandbox_image must not be empty")
        if self.sandbox_memory_mb < 64 or self.sandbox_cpus <= 0 or self.sandbox_pids_limit < 1:
            raise ValueError("sandbox resource limits are invalid")

    @property
    def can_write(self) -> bool:
        return self.permission_mode == "workspace-write"

    def resolve_workspace_path(self, raw_path: str) -> Path:
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        resolved = candidate.resolve(strict=False)
        if not self._is_within_workspace(resolved):
            raise WorkspaceViolation(f"path is outside workspace: {raw_path}")
        return resolved

    def check_write_allowed(self) -> None:
        if not self.can_write:
            raise PermissionDenied("write operation denied in read-only mode")

    def check_bash_allowed(self, command: str) -> None:
        if self.permission_mode == "read-only" and _looks_mutating(command):
            raise PermissionDenied("mutating shell command denied in read-only mode")
        if self.require_confirmation and _looks_destructive(command):
            raise PermissionDenied(
                "destructive command requires explicit user confirmation; V3 demo denies it by default"
            )

    def _is_within_workspace(self, path: Path) -> bool:
        try:
            path.relative_to(self.workspace)
            return True
        except ValueError:
            return False


def _looks_destructive(command: str) -> bool:
    tokens = _split_command(command)
    destructive = {"rm", "rmdir", "mkfs", "shutdown", "reboot", "poweroff"}
    return any(token in destructive or token.startswith("rm ") for token in tokens)


def _looks_mutating(command: str) -> bool:
    tokens = _split_command(command)
    mutating = {
        "rm",
        "rmdir",
        "mv",
        "cp",
        "touch",
        "mkdir",
        "chmod",
        "chown",
        "python",
        "python3",
        "pip",
    }
    if any(token in mutating for token in tokens):
        return True
    return any(operator in command for operator in (">", ">>", " tee ", " sed -i"))


def _split_command(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()
