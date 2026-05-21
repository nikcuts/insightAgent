"""Runtime configuration for InsightAgent V2."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentConfig:
    """Configuration values shared by CLI, task runner, and CodeAgent."""

    max_messages: int = 20
    max_tool_iterations: int = 8
    max_tool_result_chars: int = 6000
    compact_completed_turns: bool = True
    compact_tool_result_chars: int = 1200
    memory_filenames: tuple[str, ...] = ("MEMORY.md", ".codeagent.md")
