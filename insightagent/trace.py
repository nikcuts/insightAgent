"""Console trace rendering for InsightAgent V1.0."""

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
        elif event_type == "final_answer":
            self._section("FINAL ANSWER", event["content"])
        elif event_type == "memory_injected":
            self._section("MEMORY INJECTED", "files=" + ", ".join(event["files"]))
        elif event_type == "tool_output_truncated":
            self._section(
                "TOOL OUTPUT TRUNCATED",
                (
                    f"{event['name']} id={event['id']} "
                    f"original_chars={event['original_chars']} omitted_chars={event['omitted_chars']}"
                ),
            )
        elif event_type == "context_compaction":
            self._section("CONTEXT COMPACTION", f"compacted_tool_messages={event['compacted_count']}")
        elif event_type == "self_healing_repair":
            self._section(
                "SELF HEALING",
                f"repair prompted after {event['failed_tool']} id={event['tool_call_id']}",
            )
        elif event_type == "usage_recorded":
            self._section(
                "USAGE",
                (
                    f"input_tokens_est={event['input_tokens_est']} "
                    f"output_tokens_est={event['output_tokens_est']} "
                    f"total_tokens_est={event['total_tokens_est']}"
                ),
            )
        elif event_type == "session_started":
            loaded = ", ".join(event.get("loaded_config_files") or []) or "none"
            self._section(
                "SESSION",
                f"id={event['session_id']}\ndir={event['session_dir']}\nconfig_files={loaded}",
            )
        elif event_type == "tool_use_required":
            self._section(
                "TOOL USE REQUIRED",
                f"iteration={event['iteration']} model returned no tool calls; prompting for actual tool use",
            )
        elif event_type == "tool_arguments_invalid":
            self._section(
                "TOOL ARGUMENTS INVALID",
                f"{event['tool_name']}: {event['detail']}",
            )
        elif event_type == "task_phase_changed":
            self._section(
                "TASK PHASE",
                (
                    f"{event['from_phase']} -> {event['phase']} via {event['tool']} "
                    f"repairs={event['repair_attempts']} verifications={event['verification_attempts']}"
                ),
            )
        elif event_type == "mcp_config_loaded":
            loaded = ", ".join(event.get("loaded_config_files") or []) or "none"
            self._section("MCP CONFIG", f"config_files={loaded}")
        elif event_type == "mcp_server_starting":
            self._section("MCP STARTING", f"{event['server']} transport={event.get('transport')}")
        elif event_type == "mcp_server_started":
            self._section(
                "MCP STARTED",
                (
                    f"{event['server']} tools={event.get('tools', 0)} "
                    f"resources={event.get('resources', 0)} prompts={event.get('prompts', 0)}"
                ),
            )
        elif event_type == "mcp_server_failed":
            self._section("MCP FAILED", f"{event['server']}: {event.get('error', '')}")
        elif event_type == "mcp_server_stopped":
            self._section("MCP STOPPED", event["server"])

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
