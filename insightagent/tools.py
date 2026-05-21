"""V1.0 basic tools with deliberately simple behavior."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


class Tool(Protocol):
    name: str
    description: str
    input_schema: dict[str, Any]

    def run(self, arguments: dict[str, Any]) -> str:
        ...


@dataclass(frozen=True)
class ExecuteCommandTool:
    name: str = "execute_command"
    description: str = "Run a shell command and return stdout, stderr, and exit code."
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
        cwd = arguments.get("cwd")
        timeout = int(arguments.get("timeout", 60))
        completed = subprocess.run(
            command,
            shell=True,
            cwd=str(cwd) if cwd else None,
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


@dataclass(frozen=True)
class ReadFileTool:
    name: str = "read_file"
    description: str = "Read an entire UTF-8 text file and return its full contents."
    input_schema: dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "input_schema",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the text file to read."}
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        )

    def run(self, arguments: dict[str, Any]) -> str:
        return Path(str(arguments["path"])).read_text(encoding="utf-8")


@dataclass(frozen=True)
class WriteFileTool:
    name: str = "write_file"
    description: str = "Overwrite a UTF-8 text file with the provided full contents."
    input_schema: dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "input_schema",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to write."},
                    "content": {
                        "type": "string",
                        "description": "Full file content. Existing file content is replaced.",
                    },
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        )

    def run(self, arguments: dict[str, Any]) -> str:
        path = Path(str(arguments["path"]))
        path.parent.mkdir(parents=True, exist_ok=True)
        content = str(arguments["content"])
        path.write_text(content, encoding="utf-8")
        return f"wrote {len(content)} bytes to {path}"


class ToolRegistry:
    def __init__(self, tools: list[Tool] | None = None) -> None:
        resolved_tools = default_tools() if tools is None else tools
        self._tools = {tool.name: tool for tool in resolved_tools}

    def schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema,
            }
            for tool in self._tools.values()
        ]

    def run(self, name: str, arguments: dict[str, Any]) -> str:
        if name not in self._tools:
            raise KeyError(f"unknown tool: {name}")
        return self._tools[name].run(arguments)


def default_tools() -> list[Tool]:
    return [ExecuteCommandTool(), ReadFileTool(), WriteFileTool()]
