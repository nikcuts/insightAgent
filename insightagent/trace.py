"""Console trace rendering for InsightAgent V2.0."""

from __future__ import annotations

import json
from typing import Any


class ConsoleTracer:
    def __init__(self, max_chars: int = 1600) -> None:
        self.max_chars = max_chars

    def __call__(self, event: dict[str, Any]) -> None:
        event_type = event["type"]
        if event_type == "user_message":
            self._section("USER", event["content"])
        elif event_type == "model_request":
            self._section(
                "MODEL REQUEST",
                f"iteration={event['iteration']} messages={event['message_count']} tools={', '.join(event['tool_names'])}",
            )
        elif event_type == "model_response":
            content = event.get("content") or "<no text>"
            tool_calls = [self._summarize_tool_call(tool_call) for tool_call in event.get("tool_calls") or []]
            rendered_calls = json.dumps(tool_calls, ensure_ascii=False, indent=2)
            self._section("MODEL RESPONSE", f"{content}\n\ntool_calls:\n{rendered_calls}")
        elif event_type == "tool_call":
            arguments = json.dumps(self._summarize_arguments(event["arguments"]), ensure_ascii=False, indent=2)
            self._section("TOOL CALL", f"{event['name']} id={event['id']}\n{arguments}")
        elif event_type == "tool_result":
            status = "error" if event["is_error"] else "ok"
            self._section("TOOL RESULT", f"{event['name']} id={event['id']} status={status}\n{event['content']}")
        elif event_type == "memory_loaded":
            sources = event.get("sources") or []
            rendered = ", ".join(sources) if sources else "none"
            self._section("MEMORY LOADED", rendered)
        elif event_type == "tool_result_truncated":
            self._section(
                "TOOL RESULT TRUNCATED",
                (
                    f"{event['name']} id={event['id']} "
                    f"original={event['original_chars']} stored={event['stored_chars']} "
                    f"omitted={event['omitted_chars']}"
                ),
            )
        elif event_type == "history_compacted":
            self._section("HISTORY COMPACTED", f"messages={event['compacted_count']}")
        elif event_type == "final_answer":
            self._section("FINAL ANSWER", event["content"])

    def _section(self, title: str, body: str) -> None:
        print(f"\n--- {title} ---")
        print(self._clip(body))

    def _clip(self, text: str) -> str:
        if len(text) <= self.max_chars:
            return text
        head = text[: self.max_chars // 2]
        tail = text[-self.max_chars // 2 :]
        omitted = len(text) - len(head) - len(tail)
        return f"{head}\n...[trace clipped {omitted} chars]...\n{tail}"

    def _summarize_tool_call(self, tool_call: dict[str, Any]) -> dict[str, Any]:
        summarized = dict(tool_call)
        summarized["arguments"] = self._summarize_arguments(summarized.get("arguments") or {})
        return summarized

    def _summarize_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        summarized = dict(arguments)
        content = summarized.get("content")
        if isinstance(content, str) and len(content) > 240:
            summarized["content"] = {
                "chars": len(content),
                "preview": content[:160],
                "note": "full content sent to tool; trace display summarized",
            }
        return summarized
