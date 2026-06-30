"""Core V5.0 agent loop with runtime state."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from .context import ContextManager
from .memory import SlidingWindowMemory
from ..api.messages import Message
from ..api.providers import ModelClient, ToolArgumentsParseError
from ..api.resilience import ToolCallExtractor, build_repair_prompt
from .session import Session, SessionStore
from .task_state import TaskPhase, TaskState, mark_final_answer, phase_instruction, transition_after_tool
from ..tools import ToolRegistry
from ..telemetry.usage import UsageTracker


DEFAULT_SYSTEM_PROMPT = """You are InsightAgent V5.0, a coding agent runtime with sessions, usage tracking, grep search, and self-healing repair loops.
Use tools when needed. After writing or editing code, call `run_verification` to test/compile the project, and keep editing and re-verifying until it passes.
Be direct, and report tool errors clearly."""

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
        context_manager: ContextManager | None = None,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        max_tool_iterations: int = 8,
        session_store: SessionStore | None = None,
        session: Session | None = None,
        usage_tracker: UsageTracker | None = None,
        require_tool_use: bool = False,
        task_state: TaskState | None = None,
        tool_call_extractor: ToolCallExtractor | None = None,
        max_nudge_attempts: int = 2,
        resilience_enabled: bool = True,
        max_wall_seconds: float | None = None,
        delta_handler: Callable[[str], None] | None = None,
    ) -> None:
        self.model_client = model_client
        self.delta_handler = delta_handler
        self.tools = tools or ToolRegistry()
        self.memory = memory or SlidingWindowMemory(max_messages=20)
        self.context_manager = context_manager or ContextManager()
        self.max_tool_iterations = max_tool_iterations
        self.session_store = session_store
        self.session = session
        self.usage_tracker = usage_tracker or UsageTracker()
        self.require_tool_use = require_tool_use
        self.task_state = task_state or TaskState()
        self.tool_call_extractor = tool_call_extractor or ToolCallExtractor()
        self.max_nudge_attempts = max_nudge_attempts
        self.resilience_enabled = resilience_enabled
        # Wall-clock budget for a single turn. None/<=0 disables the budget.
        self.max_wall_seconds = max_wall_seconds if (max_wall_seconds or 0) > 0 else None
        self.current_task = ""
        if session is not None and session.messages:
            self.messages = list(session.messages)
        else:
            self.messages: list[Message] = [Message(role="system", content=system_prompt)]
            if self.session is not None:
                self.session.messages = list(self.messages)
                self._persist_session()

    def run_turn(self, user_input: str) -> AgentResult:
        return self.run_turn_with_trace(user_input, trace=None)

    def run_turn_with_trace(self, user_input: str, trace: TraceHandler | None = None) -> AgentResult:
        if self.task_state.phase in {TaskPhase.DONE, TaskPhase.FAILED}:
            self.task_state = TaskState()
        self.current_task = user_input
        self.messages.append(Message(role="user", content=user_input))
        self._sync_session()
        self._emit(trace, {"type": "user_message", "content": user_input})
        saw_tool_call = False
        pending_repair = False
        nudge_attempts = 0
        next_tool_choice: dict[str, Any] | None = None
        deadline = (
            time.monotonic() + self.max_wall_seconds if self.max_wall_seconds is not None else None
        )

        for iteration in range(1, self.max_tool_iterations + 1):
            if deadline is not None and time.monotonic() >= deadline:
                return self._finalize_time_budget_exceeded(iteration, trace)
            self.messages = self.memory.trim(self.messages)
            request_messages = self.messages + [Message(role="user", content=phase_instruction(self.task_state))]
            self._emit(
                trace,
                {
                    "type": "model_request",
                    "iteration": iteration,
                    "message_count": len(request_messages),
                    "phase": self.task_state.phase.value,
                    "tool_names": [tool["name"] for tool in self.tools.schemas()],
                },
            )
            active_tool_choice = next_tool_choice
            next_tool_choice = None
            try:
                response = self._model_complete(request_messages, self.tools.schemas(), active_tool_choice)
            except ToolArgumentsParseError as error:
                pending_repair = True
                parse_error_message = (
                    f"ToolArgumentsParseError: {error}. The model must call tools with valid JSON "
                    "arguments. For large source files, escape newlines and quotes correctly in the "
                    "write_file content string, or split work across smaller files/tool calls."
                )
                self.messages.append(
                    Message(
                        role="tool",
                        content=parse_error_message,
                        tool_call_id="invalid_tool_arguments",
                        is_error=True,
                    )
                )
                self._transition_task_phase("invalid_tool_arguments", parse_error_message, True, trace)
                self.messages.append(
                    Message(
                        role="user",
                        content=(
                            "Repair mode: your previous tool call had malformed JSON arguments. "
                            "Do not output markdown-only examples. Retry with a real tool call whose arguments are valid JSON."
                        ),
                    )
                )
                self._emit(
                    trace,
                    {
                        "type": "tool_arguments_invalid",
                        "tool_name": error.tool_name,
                        "detail": error.detail,
                    },
                )
                continue
            usage_sample = self.usage_tracker.record_model_call(
                request_messages,
                response,
                model=getattr(self.model_client, "model", None),
            )
            self._emit(
                trace,
                {
                    "type": "usage_recorded",
                    "input_tokens_est": usage_sample.input_tokens_est,
                    "output_tokens_est": usage_sample.output_tokens_est,
                    "total_tokens_est": usage_sample.input_tokens_est + usage_sample.output_tokens_est,
                    "is_estimated": usage_sample.is_estimated,
                    "cost_usd": usage_sample.cost_usd,
                },
            )
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
            self._sync_session()

            if not response.tool_calls:
                failed_phase = self.task_state.phase == TaskPhase.FAILED
                recovered = []
                if self.resilience_enabled and self.require_tool_use and not failed_phase:
                    recovered = self.tool_call_extractor.extract(
                        response.content,
                        {tool["name"] for tool in self.tools.schemas()},
                        task=self.current_task,
                    )
                if recovered:
                    response.tool_calls = [item.tool_call for item in recovered]
                    assistant_message.tool_calls = response.tool_calls
                    self._sync_session()
                    for item in recovered:
                        self._emit(
                            trace,
                            {
                                "type": "tool_call_recovered",
                                "id": item.tool_call.id,
                                "name": item.tool_call.name,
                                "source": item.source,
                            },
                        )
                    nudge_attempts = 0
                    # Fall through to the tool-execution loop with the recovered calls.
                else:
                    # The task has implemented something but has not yet reached a
                    # successful verification (SUMMARIZE). A weak model often stops here
                    # with a "next I will..." message and no tool call; treat that as
                    # unfinished and keep steering it toward run_verification instead of
                    # accepting the dangling text as the final answer.
                    needs_verification = self.task_state.phase in {
                        TaskPhase.IMPLEMENT,
                        TaskPhase.REPAIR,
                    }
                    if self.resilience_enabled:
                        should_nudge = (
                            self.require_tool_use
                            and not failed_phase
                            and (not saw_tool_call or pending_repair or needs_verification)
                            and nudge_attempts < self.max_nudge_attempts
                        )
                    else:
                        # Legacy behaviour: nudge whenever a tool is required, with no
                        # FAILED exemption and no attempt cap (reproduces the deadlock).
                        should_nudge = self.require_tool_use and (not saw_tool_call or pending_repair)
                    if should_nudge:
                        nudge_attempts += 1
                        if self.resilience_enabled:
                            forced_tool = self._forced_tool_for_phase()
                            if nudge_attempts >= 2 and forced_tool is not None:
                                next_tool_choice = {"force_tool": forced_tool}
                            nudge_content = self._nudge_prompt(nudge_attempts, forced_tool, needs_verification)
                        else:
                            forced_tool = None
                            nudge_content = (
                                "You returned no tool calls, but this task still requires actual tool execution. "
                                "Do not describe tool calls in markdown. Call the available tools directly now. "
                                "If you are repairing malformed tool JSON, retry with smaller valid JSON tool calls."
                            )
                        self.messages.append(Message(role="user", content=nudge_content))
                        self._sync_session()
                        self._emit(
                            trace,
                            {
                                "type": "tool_use_required",
                                "iteration": iteration,
                                "attempt": nudge_attempts,
                                "forced_tool": forced_tool if next_tool_choice else None,
                            },
                        )
                        continue
                    compaction = self.context_manager.compact_after_completion(self.messages)
                    self.messages = compaction.messages
                    if compaction.compacted_count:
                        self._emit(
                            trace,
                            {
                                "type": "context_compaction",
                                "compacted_count": compaction.compacted_count,
                            },
                        )
                    self._mark_task_finished(trace)
                    self.messages = self.memory.trim(self.messages)
                    self._emit(trace, {"type": "final_answer", "content": response.content, "iterations": iteration})
                    self._sync_session()
                    return AgentResult(
                        content=response.content,
                        messages=list(self.messages),
                        iterations=iteration,
                    )

            for tool_call in response.tool_calls:
                saw_tool_call = True
                nudge_attempts = 0
                self._emit(
                    trace,
                    {
                        "type": "tool_call",
                        "id": tool_call.id,
                        "name": tool_call.name,
                        "arguments": tool_call.arguments,
                    },
                )
                execution_result = self.tools.execute(tool_call.name, tool_call.arguments)
                tool_result = Message(
                    role="tool",
                    content=execution_result.content,
                    tool_call_id=tool_call.id,
                    is_error=execution_result.is_error,
                )
                if execution_result.suppressed:
                    self._emit(
                        trace,
                        {
                            "type": "tool_call_suppressed",
                            "id": tool_call.id,
                            "name": tool_call.name,
                            "failure_kind": _failure_kind_value(execution_result.failure_kind),
                            "retryable": execution_result.retryable,
                            "repeat_count": execution_result.repeat_count,
                        },
                    )
                truncation = self.context_manager.truncate_tool_output(tool_result.content)
                if truncation.truncated:
                    tool_result.content = truncation.content
                    self._emit(
                        trace,
                        {
                            "type": "tool_output_truncated",
                            "id": tool_call.id,
                            "name": tool_call.name,
                            "original_chars": truncation.original_chars,
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
                        "failure_kind": _failure_kind_value(execution_result.failure_kind),
                        "retryable": execution_result.retryable,
                        "permission": execution_result.permission.value,
                        "risk": execution_result.risk.value,
                        "command_kind": execution_result.command_kind,
                        "suppressed": execution_result.suppressed,
                    },
                )
                self.messages.append(tool_result)
                self._transition_task_phase(tool_call.name, tool_result.content, tool_result.is_error, trace)
                self._sync_session()
                if tool_result.is_error:
                    pending_repair = True
                    if self.resilience_enabled:
                        repair_prompt = build_repair_prompt(execution_result, self.current_task)
                    else:
                        if execution_result.retryable:
                            retry_guidance = "You may retry if the next call changes strategy or arguments."
                        else:
                            retry_guidance = "Do not repeat the same tool call unchanged; the runtime marked it non-retryable."
                        repair_prompt = (
                            "A tool call failed. Enter repair mode: inspect the error, adjust the plan, "
                            "and use tools to fix or verify the issue without asking the user first. "
                            f"failure_kind={_failure_kind_value(execution_result.failure_kind)}. "
                            f"{retry_guidance} {execution_result.repair_guidance}"
                        )
                    self.messages.append(Message(role="user", content=repair_prompt))
                    self._sync_session()
                    self._emit(
                        trace,
                        {
                            "type": "self_healing_repair",
                            "failed_tool": tool_call.name,
                            "tool_call_id": tool_call.id,
                            "failure_kind": _failure_kind_value(execution_result.failure_kind),
                            "retryable": execution_result.retryable,
                            "backoff_delay": execution_result.metadata.get("backoff_delay"),
                        },
                    )
                else:
                    pending_repair = False

        warning = "Stopped after reaching the V5.0 max tool-iteration limit."
        self.messages.append(Message(role="assistant", content=warning))
        self.task_state.phase = TaskPhase.FAILED
        self._sync_session()
        compaction = self.context_manager.compact_after_completion(self.messages)
        self.messages = compaction.messages
        if compaction.compacted_count:
            self._emit(trace, {"type": "context_compaction", "compacted_count": compaction.compacted_count})
        self.messages = self.memory.trim(self.messages)
        self._emit(trace, {"type": "final_answer", "content": warning, "iterations": self.max_tool_iterations})
        self._sync_session()
        return AgentResult(content=warning, messages=list(self.messages), iterations=self.max_tool_iterations)

    def _finalize_time_budget_exceeded(self, iteration: int, trace: TraceHandler | None) -> AgentResult:
        """Stop the turn cleanly when the wall-clock budget is exhausted.

        Without this guard a weak model that keeps producing slow, unusable
        responses can stall a turn for many minutes. We stop deterministically,
        mark the task FAILED, and return a text explanation instead of hanging.
        """
        budget = self.max_wall_seconds
        warning = (
            "Stopped: exceeded the wall-clock time budget"
            + (f" ({budget:.0f}s)" if budget is not None else "")
            + " for this turn before the task finished. This usually means the model was too slow or "
            "kept producing unusable responses. Partial progress (if any) is preserved in the workspace."
        )
        self.messages.append(Message(role="assistant", content=warning))
        self.task_state.phase = TaskPhase.FAILED
        self._sync_session()
        compaction = self.context_manager.compact_after_completion(self.messages)
        self.messages = compaction.messages
        if compaction.compacted_count:
            self._emit(trace, {"type": "context_compaction", "compacted_count": compaction.compacted_count})
        self.messages = self.memory.trim(self.messages)
        self._emit(
            trace,
            {
                "type": "time_budget_exceeded",
                "iteration": iteration,
                "max_wall_seconds": budget,
            },
        )
        self._emit(trace, {"type": "final_answer", "content": warning, "iterations": iteration})
        self._sync_session()
        return AgentResult(content=warning, messages=list(self.messages), iterations=iteration)

    def _model_complete(self, messages: list[Message], schemas: list[dict[str, Any]], tool_choice: Any | None):
        kwargs: dict[str, Any] = {}
        if tool_choice is not None:
            kwargs["tool_choice"] = tool_choice
        if self.delta_handler is not None:
            kwargs["on_delta"] = self.delta_handler
        if not kwargs:
            return self.model_client.complete(messages, schemas)
        try:
            return self.model_client.complete(messages, schemas, **kwargs)
        except TypeError:
            # Model client predates tool_choice/on_delta; degrade gracefully.
            if tool_choice is not None:
                try:
                    return self.model_client.complete(messages, schemas, tool_choice=tool_choice)
                except TypeError:
                    pass
            return self.model_client.complete(messages, schemas)

    def _forced_tool_for_phase(self) -> str | None:
        available = {tool["name"] for tool in self.tools.schemas()}
        if self.task_state.phase == TaskPhase.VERIFY:
            for name in ("run_verification", "execute_command"):
                if name in available:
                    return name
        for name in ("write_file", "edit_file", "run_verification", "execute_command"):
            if name in available:
                return name
        return next(iter(sorted(available)), None)

    def _nudge_prompt(self, attempt: int, forced_tool: str | None, needs_verification: bool = False) -> str:
        verification_reminder = (
            " You have written or edited code but have NOT verified it yet. Do not stop or only "
            "describe the next step: take the next concrete action (write any remaining files, then "
            "call `run_verification`) and keep going until run_verification reports exit_code 0."
            if needs_verification
            else ""
        )
        if attempt >= 2 and forced_tool is not None:
            return (
                "You still returned no tool call. This task requires real tool execution, not a description. "
                f"You MUST now call the `{forced_tool}` tool with valid JSON arguments. "
                "Do not put the answer only in prose or markdown. If your runtime cannot emit a native tool "
                "call, output exactly one line in this protocol and nothing else: "
                f"<tool_call>{{\"name\": \"{forced_tool}\", \"arguments\": {{...}}}}</tool_call>"
                + verification_reminder
            )
        return (
            "You returned no tool calls, but this task requires actual tool execution. Call an available tool "
            "directly now with valid JSON arguments. Do not put the final code only in prose or markdown. "
            "Example of the textual tool-call protocol the runtime accepts: "
            "<tool_call>{\"name\": \"write_file\", \"arguments\": {\"path\": \"main.py\", \"content\": \"...\"}}</tool_call>"
            + verification_reminder
        )

    def _execute_tool_call(self, tool_call_id: str, name: str, arguments: dict[str, Any]) -> Message:
        try:
            content = self.tools.run(name, arguments)
            is_error = name == "execute_command" and not content.startswith("exit_code: 0\n")
            return Message(role="tool", content=content, tool_call_id=tool_call_id, is_error=is_error)
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

    def _transition_task_phase(
        self,
        tool_name: str,
        tool_content: str,
        is_error: bool,
        trace: TraceHandler | None,
    ) -> None:
        old_phase, new_phase = transition_after_tool(self.task_state, tool_name, tool_content, is_error)
        if old_phase != new_phase:
            self._emit(
                trace,
                {
                    "type": "task_phase_changed",
                    "from_phase": old_phase.value,
                    "phase": new_phase.value,
                    "tool": tool_name,
                    "repair_attempts": self.task_state.repair_attempts,
                    "verification_attempts": self.task_state.verification_attempts,
                },
            )
            self._sync_session()

    def _mark_task_finished(self, trace: TraceHandler | None) -> None:
        old_phase, new_phase = mark_final_answer(self.task_state)
        if old_phase != new_phase:
            self._emit(
                trace,
                {
                    "type": "task_phase_changed",
                    "from_phase": old_phase.value,
                    "phase": new_phase.value,
                    "tool": "final_answer",
                    "repair_attempts": self.task_state.repair_attempts,
                    "verification_attempts": self.task_state.verification_attempts,
                },
            )
            self._sync_session()

    def compact_history(self, preserve_recent_messages: int = 4) -> dict[str, int]:
        if len(self.messages) <= preserve_recent_messages + 1:
            return {"removed_message_count": 0}
        system_messages = [message for message in self.messages if message.role == "system"]
        non_system_messages = [message for message in self.messages if message.role != "system"]
        removed = non_system_messages[:-preserve_recent_messages]
        preserved = non_system_messages[-preserve_recent_messages:]
        summary = self._summarize_removed_messages(removed)
        self.messages = system_messages[:1] + [Message(role="system", content=summary)] + preserved
        self._sync_session()
        return {"removed_message_count": len(removed)}

    def clear_history(self) -> None:
        system_messages = [message for message in self.messages if message.role == "system"]
        self.messages = system_messages[:1]
        self._sync_session()

    def _summarize_removed_messages(self, messages: list[Message]) -> str:
        role_counts: dict[str, int] = {}
        tool_names = []
        for message in messages:
            role_counts[message.role] = role_counts.get(message.role, 0) + 1
            for tool_call in message.tool_calls:
                tool_names.append(tool_call.name)
        unique_tools = sorted(set(tool_names))
        lines = [
            "InsightAgent V5 compacted earlier conversation.",
            f"Removed messages: {len(messages)}.",
            "Role counts: " + ", ".join(f"{role}={count}" for role, count in sorted(role_counts.items())),
        ]
        if unique_tools:
            lines.append("Tools requested: " + ", ".join(unique_tools))
        return "\n".join(lines)

    def _sync_session(self) -> None:
        if self.session is not None:
            self.session.messages = list(self.messages)
            self.session.metadata["usage"] = {
                "turns": self.usage_tracker.turns,
                "input_tokens_est": self.usage_tracker.total_input_tokens_est,
                "output_tokens_est": self.usage_tracker.total_output_tokens_est,
                "total_tokens_est": self.usage_tracker.total_tokens_est,
                "cost_usd": self.usage_tracker.total_cost_usd,
                "source": self.usage_tracker.source,
            }
            self.session.metadata["task_state"] = {
                "phase": self.task_state.phase.value,
                "verification_attempts": self.task_state.verification_attempts,
                "repair_attempts": self.task_state.repair_attempts,
                "last_error": self.task_state.last_error,
            }
            self._persist_session()

    def _persist_session(self) -> None:
        if self.session_store is not None and self.session is not None:
            self.session_store.save(self.session)


def _failure_kind_value(kind: Any | None) -> str | None:
    if kind is None:
        return None
    return getattr(kind, "value", str(kind))
