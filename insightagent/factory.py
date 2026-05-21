"""Shared agent assembly helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from .agent import DEFAULT_SYSTEM_PROMPT, CodeAgent
from .config import AgentConfig
from .context import build_system_prompt
from .memory import SlidingWindowMemory
from .project_memory import load_project_memory
from .providers import ModelClient
from .tools import ToolRegistry

TraceHandler = Callable[[dict[str, object]], None]


def build_agent_with_memory(
    model_client: ModelClient,
    workspace: Path,
    config: AgentConfig | None = None,
    base_system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    tools: ToolRegistry | None = None,
    trace: TraceHandler | None = None,
) -> CodeAgent:
    resolved_config = config or AgentConfig()
    project_memory = load_project_memory(workspace, resolved_config.memory_filenames)
    if trace is not None:
        trace({"type": "memory_loaded", "sources": project_memory.source_names()})
    system_prompt = build_system_prompt(base_system_prompt, project_memory)
    return CodeAgent(
        model_client,
        tools=tools,
        memory=SlidingWindowMemory(max_messages=resolved_config.max_messages),
        system_prompt=system_prompt,
        config=resolved_config,
    )
