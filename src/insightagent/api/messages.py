"""Shared message and tool-call primitives for InsightAgent."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolCall:
    """A model-requested tool invocation."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Message:
    """Provider-neutral conversation message."""

    role: str
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    is_error: bool = False


@dataclass
class ModelResponse:
    """Provider-neutral model response."""

    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
