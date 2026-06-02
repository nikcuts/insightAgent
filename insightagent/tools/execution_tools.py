"""Shell execution tools."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Any

from ..tool_context import ToolContext


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
        command = str(arguments["command"])
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
