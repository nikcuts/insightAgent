"""V1.0 sliding-window memory."""

from __future__ import annotations

from ..api.messages import Message


class SlidingWindowMemory:
    """Keep system prompts plus the latest N non-system messages."""

    def __init__(self, max_messages: int = 20) -> None:
        self.max_messages = max_messages

    def trim(self, messages: list[Message]) -> list[Message]:
        system_messages = [message for message in messages if message.role == "system"]
        non_system_messages = [message for message in messages if message.role != "system"]
        return system_messages + non_system_messages[-self.max_messages :]
