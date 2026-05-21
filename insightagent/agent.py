"""Core V1.0 agent loop."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .compaction import compact_completed_turn, compact_tool_result
from .config import AgentConfig
from .memory import SlidingWindowMemory
from .messages import Message
from .providers import ModelClient
from .tools import ToolRegistry


DEFAULT_SYSTEM_PROMPT = """You are InsightAgent V1.0, a baseline coding agent.
Use tools when needed. Be direct, and report tool errors clearly."""

TraceHandler = Callable[[dict[str, Any]], None]


@dataclass
class AgentResult:
    content: str
    messages: list[Message]
    iterations: int


class CodeAgent:
    def __init__(
        self,
        model_client: ModelClient,
        tools: ToolRegistry | None = None,
        memory: SlidingWindowMemory | None = None,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        max_tool_iterations: int | None = None,
        config: AgentConfig | None = None,
        max_tool_result_chars: int | None = None,
        compact_completed_turns: bool | None = None,
        compact_tool_result_chars: int | None = None,
    ) -> None:
        self.config = config or AgentConfig()
        self.model_client = model_client
        self.tools = tools or ToolRegistry()
        self.memory = memory or SlidingWindowMemory(max_messages=self.config.max_messages)
        self.max_tool_iterations = max_tool_iterations or self.config.max_tool_iterations
        self.max_tool_result_chars = max_tool_result_chars or self.config.max_tool_result_chars
        self.compact_completed_turns = (
            self.config.compact_completed_turns if compact_completed_turns is None else compact_completed_turns
        )
        self.compact_tool_result_chars = compact_tool_result_chars or self.config.compact_tool_result_chars
        self.messages: list[Message] = [Message(role="system", content=system_prompt)]

    def run_turn(self, user_input: str) -> AgentResult:
        return self.run_turn_with_trace(user_input, trace=None)

    def run_turn_with_trace(self, user_input: str, trace: TraceHandler | None = None) -> AgentResult:
        self.messages.append(Message(role="user", content=user_input))
        self._emit(trace, {"type": "user_message", "content": user_input})

        for iteration in range(1, self.max_tool_iterations + 1):
            self.messages = self.memory.trim(self.messages)
            self._emit(
                trace,
                {
                    "type": "model_request",
                    "iteration": iteration,
                    "message_count": len(self.messages),
                    "tool_names": [tool["name"] for tool in self.tools.schemas()],
                },
            )
            response = self.model_client.complete(self.messages, self.tools.schemas())
            self._emit(
                trace,
                {
                    "type": "model_response",
                    "iteration": iteration,
                    "content": response.content,
                    "tool_calls": [
                        {"id": tool_call.id, "name": tool_call.name, "arguments": tool_call.arguments}
                        for tool_call in response.tool_calls
                    ],
                },
            )
            assistant_message = Message(
                role="assistant",
                content=response.content,
                tool_calls=response.tool_calls,
            )
            self.messages.append(assistant_message)

            if not response.tool_calls:
                self.messages = self.memory.trim(self.messages)
                self._compact_completed_turn(trace)
                self._emit(trace, {"type": "final_answer", "content": response.content, "iterations": iteration})
                return AgentResult(
                    content=response.content,
                    messages=list(self.messages),
                    iterations=iteration,
                )

            for tool_call in response.tool_calls:
                self._emit(
                    trace,
                    {
                        "type": "tool_call",
                        "id": tool_call.id,
                        "name": tool_call.name,
                        "arguments": tool_call.arguments,
                    },
                )
                raw_tool_result = self._execute_tool_call(tool_call.id, tool_call.name, tool_call.arguments)
                tool_result, truncation = compact_tool_result(raw_tool_result, self.max_tool_result_chars)
                if truncation.truncated:
                    self._emit(
                        trace,
                        {
                            "type": "tool_result_truncated",
                            "id": tool_call.id,
                            "name": tool_call.name,
                            "original_chars": truncation.original_chars,
                            "stored_chars": truncation.stored_chars,
                            "omitted_chars": truncation.omitted_chars,
                        },
                    )
                self._emit(
                    trace,
                    {
                        "type": "tool_result",
                        "id": tool_call.id,
                        "name": tool_call.name,
                        "is_error": tool_result.is_error,
                        "content": tool_result.content,
                    },
                )
                self.messages.append(tool_result)

        warning = "Stopped after reaching the V1.0 max tool-iteration limit."
        self.messages.append(Message(role="assistant", content=warning))
        self.messages = self.memory.trim(self.messages)
        self._compact_completed_turn(trace)
        self._emit(trace, {"type": "final_answer", "content": warning, "iterations": self.max_tool_iterations})
        return AgentResult(content=warning, messages=list(self.messages), iterations=self.max_tool_iterations)

    def _execute_tool_call(self, tool_call_id: str, name: str, arguments: dict[str, Any]) -> Message:
        try:
            content = self.tools.run(name, arguments)
            return Message(role="tool", content=content, tool_call_id=tool_call_id, is_error=False)
        except Exception as error:  # V1.0 sends raw tool failures back to the model.
            return Message(
                role="tool",
                content=f"{type(error).__name__}: {error}",
                tool_call_id=tool_call_id,
                is_error=True,
            )

    def _emit(self, trace: TraceHandler | None, event: dict[str, Any]) -> None:
        if trace is not None:
            trace(event)

    def _compact_completed_turn(self, trace: TraceHandler | None) -> None:
        if not self.compact_completed_turns:
            return
        self.messages, compacted_count = compact_completed_turn(self.messages, self.compact_tool_result_chars)
        if compacted_count:
            self._emit(trace, {"type": "history_compacted", "compacted_count": compacted_count})
