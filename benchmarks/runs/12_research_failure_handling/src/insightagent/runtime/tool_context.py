"""Tool context, workspace boundaries, and permission checks for V3.0."""

from __future__ import annotations

import fnmatch
import shlex
from dataclasses import dataclass
from pathlib import Path


class PermissionDenied(RuntimeError):
    """Raised when a tool violates the current permission policy."""


class WorkspaceViolation(RuntimeError):
    """Raised when a tool attempts to access a path outside the workspace."""


@dataclass(frozen=True)
class ToolContext:
    workspace: Path
    permission_mode: str = "workspace-write"
    require_confirmation: bool = True
    max_read_chars: int = 500_000
    max_write_chars: int = 500_000
    # Glob patterns (matched against workspace-relative posix paths and basenames)
    # that write tools must refuse to modify. Used to make e.g. test files
    # read-only so the agent fixes the code under test instead of the tests.
    protected_globs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "workspace", self.workspace.expanduser().resolve())
        if self.permission_mode not in {"read-only", "workspace-write"}:
            raise ValueError("permission_mode must be read-only or workspace-write")
        object.__setattr__(self, "protected_globs", tuple(self.protected_globs))

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

    def check_path_writable(self, path: Path) -> None:
        """Refuse writes to protected paths (e.g. test files).

        Matches each protected glob against both the workspace-relative posix
        path and the file's basename, so patterns like ``tests/*`` and
        ``test_*.py`` both work.
        """

        if not self.protected_globs:
            return
        resolved = path.expanduser().resolve(strict=False)
        try:
            rel = resolved.relative_to(self.workspace).as_posix()
        except ValueError:
            return
        basename = resolved.name
        for pattern in self.protected_globs:
            if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(basename, pattern):
                raise PermissionDenied(
                    f"path is protected and must not be modified: {rel}. "
                    "Fix the code under test instead of changing the tests."
                )

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
