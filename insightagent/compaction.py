"""Context truncation and compaction helpers for InsightAgent V2."""

from __future__ import annotations

from dataclasses import dataclass

from .messages import Message


@dataclass(frozen=True)
class TruncationResult:
    text: str
    truncated: bool
    original_chars: int
    stored_chars: int
    omitted_chars: int = 0


def truncate_text(text: str, max_chars: int) -> TruncationResult:
    original_chars = len(text)
    if max_chars <= 0 or original_chars <= max_chars:
        return TruncationResult(text=text, truncated=False, original_chars=original_chars, stored_chars=original_chars)

    marker_template = "\n\n...[truncated {omitted} chars]...\n\n"
    marker = marker_template.format(omitted=0)
    available = max(max_chars - len(marker), 18)
    head_chars = available // 2
    tail_chars = available - head_chars
    omitted = original_chars - head_chars - tail_chars
    marker = marker_template.format(omitted=omitted)
    compacted = f"{text[:head_chars]}{marker}{text[-tail_chars:]}"
    return TruncationResult(
        text=compacted,
        truncated=True,
        original_chars=original_chars,
        stored_chars=len(compacted),
        omitted_chars=omitted,
    )


def compact_tool_result(message: Message, max_chars: int) -> tuple[Message, TruncationResult]:
    result = truncate_text(message.content, max_chars)
    if not result.truncated:
        return message, result
    return (
        Message(
            role=message.role,
            content=result.text,
            tool_calls=list(message.tool_calls),
            tool_call_id=message.tool_call_id,
            is_error=message.is_error,
        ),
        result,
    )


def compact_completed_turn(messages: list[Message], max_chars: int) -> tuple[list[Message], int]:
    compacted_messages: list[Message] = []
    compacted_count = 0
    for message in messages:
        if message.role != "tool":
            compacted_messages.append(message)
            continue
        compacted, result = compact_tool_result(message, max_chars)
        if result.truncated:
            compacted_count += 1
        compacted_messages.append(compacted)
    return compacted_messages, compacted_count
