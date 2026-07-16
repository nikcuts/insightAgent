"""LangGraph nodes for the repository repair workflow."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import cast

from langchain_core.messages import (
    AnyMessage,
    AIMessage,
    BaseMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.runnables import Runnable, RunnableConfig
from langchain_core.tools import BaseTool

from insightagent.graph.repository_snapshot import build_repository_snapshot
from insightagent.graph.contracts import TaskContract, command_matches_required, extract_task_contract
from insightagent.graph.observability import GraphObservability, sanitize_for_model_trace_and_persistence
from insightagent.graph.state import AgentState, JSONValue, trim_message_prefix
from insightagent.graph.tools import ContractAwareToolInvoker, remaining_seconds_from_config
from insightagent.graph.usage import UsageAccumulator
from insightagent.runtime.tool_context import ToolContext
from insightagent.runtime.types import ToolSpec


_INSPECTION_TOOLS = {
    "read_file",
    "grep_search",
    "glob_search",
    "git_status",
    "git_diff",
    "lsp_diagnostics",
    "parse_ast",
    "get_function_signature",
    "find_dependencies",
    "get_code_metrics",
}
_EXIT_CODE_PATTERN = re.compile(r"exit_code:\s*(-?\d+)")


@dataclass(frozen=True)
class GraphServices:
    """Runtime-only collaborators used to construct one compiled graph."""

    model: Runnable
    tools: Mapping[str, BaseTool]
    tool_specs: Mapping[str, ToolSpec]
    tool_context: ToolContext
    tool_invoker: ContractAwareToolInvoker
    max_iterations: int = 8
    max_repair_attempts: int = 3
    max_tool_output_chars: int = 8_000
    compact_tool_output_chars: int = 600
    max_context_messages: int = 24
    project_memory: str = ""
    language: str = "zh-CN"
    observability: GraphObservability | None = None
    _tools: dict[str, BaseTool] = field(init=False, repr=False, compare=False)
    _tool_specs: dict[str, ToolSpec] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_tools", dict(self.tools))
        object.__setattr__(self, "_tool_specs", dict(self.tool_specs))
        if self.max_iterations < 1:
            raise ValueError("max_iterations must be positive")
        if self.max_repair_attempts < 0:
            raise ValueError("max_repair_attempts must not be negative")
        if self.max_tool_output_chars < 1:
            raise ValueError("max_tool_output_chars must be positive")
        if self.compact_tool_output_chars < 1:
            raise ValueError("compact_tool_output_chars must be positive")
        if self.max_context_messages < 1:
            raise ValueError("max_context_messages must be positive")

    def initial_state(self, task: str) -> AgentState:
        from insightagent.graph.state import new_turn_update

        return {
            **new_turn_update(task),
            "workspace": str(self.tool_context.workspace),
            "max_iterations": self.max_iterations,
            "usage": {},
        }


async def prepare_task(state: AgentState, config: RunnableConfig, services: GraphServices) -> AgentState:
    del config
    with _node_span(services, "prepare_task"):
        task = state.get("task") or _latest_human_content(state.get("messages", []))
    contract = extract_task_contract(task)
    context_parts = ["以工具结果为准，不要把未执行的修改当作完成。"]
    if services.project_memory:
        context_parts.append(str(sanitize_for_model_trace_and_persistence(services.project_memory)))
    if services.language:
        context_parts.append(f"请使用 {services.language} 输出。")
    return {
        "task": task,
        "phase": "inspect",
        "verification_command": contract.expected_verification_command,
        "messages": [SystemMessage(content="\n".join(context_parts))],
        "phase_history": _append_phase(state, "inspect"),
    }


async def inject_repository_snapshot(
    state: AgentState, config: RunnableConfig, services: GraphServices
) -> AgentState:
    del config
    with _node_span(services, "inject_repository_snapshot"):
        snapshot = build_repository_snapshot(services.tool_context.workspace)
    return {
        "messages": [
            SystemMessage(content=str(sanitize_for_model_trace_and_persistence(snapshot.content)))
        ]
    }


async def call_model(state: AgentState, config: RunnableConfig, services: GraphServices) -> AgentState:
    iteration = int(state.get("iteration", 0))
    if iteration >= min(int(state.get("max_iterations", services.max_iterations)), services.max_iterations):
        return _failure_update(state, "iteration_limit", "达到工具循环上限，未能完成任务。")
    remaining = remaining_seconds_from_config(config)
    if remaining is not None and remaining <= 0:
        return _failure_update(state, "time_budget_exceeded", "回合时间预算已耗尽。")
    try:
        with _node_span(services, "call_model"):
            response = await _await_model(
                services.model,
                _messages_for_model(state.get("messages", [])),
                _model_config(config, services.observability),
                remaining,
            )
    except asyncio.TimeoutError:
        return _failure_update(state, "time_budget_exceeded", "模型调用超过回合时间预算。")
    except Exception as error:
        return _failure_update(state, "model_error", f"{type(error).__name__}: {error}")
    if not isinstance(response, AIMessage):
        response = AIMessage(content=str(response))
    response = _sanitize_ai_message(response)
    return {
        "messages": [response],
        "iteration": iteration + 1,
        "usage": _merge_usage(state.get("usage", {}), response),
    }


async def execute_tools(state: AgentState, config: RunnableConfig, services: GraphServices) -> AgentState:
    message = _latest_ai_message(state.get("messages", []))
    if message is None:
        return _failure_update(state, "tool_protocol_error", "模型未返回可执行的工具调用。")
    contract = extract_task_contract(state.get("task", ""))
    inspected = list(state.get("inspected_files", []))
    changed = list(state.get("changed_files", []))
    workspace_revision = int(state.get("workspace_revision", 0))
    verified_workspace_revision = state.get("verified_workspace_revision")
    if not isinstance(verified_workspace_revision, int):
        verified_workspace_revision = None
    verification_attempts = list(state.get("verification_attempts", []))
    tool_events = list(state.get("tool_events", []))
    phase_history = list(state.get("phase_history", []))
    tool_messages: list[AnyMessage] = []
    phase = state.get("phase", "inspect")
    last_error: str | None = state.get("last_tool_error")
    repair_action_completed = bool(state.get("repair_action_completed", False))
    repair_inspected_since_failure = bool(state.get("repair_inspected_since_failure", False))
    batch_failed = False

    for tool_call in message.tool_calls:
        name = str(tool_call.get("name", ""))
        arguments = _tool_arguments(tool_call.get("args", {}))
        tool_call_id = str(tool_call.get("id", name))
        tool = services._tools.get(name)
        spec = services._tool_specs.get(name)
        skipped_after_error = batch_failed
        verification_rejection: str | None = None
        if skipped_after_error:
            payload = _tool_batch_aborted_payload(spec, name, arguments)
        else:
            verification_rejection = _verification_rejection(contract, name, arguments, changed)
            if verification_rejection is not None:
                payload = _verification_rejection_payload(
                    spec, name, arguments, verification_rejection
                )
            elif tool is None or spec is None:
                payload = _unknown_tool_payload(name, arguments)
            else:
                with _node_span(services, f"tool:{name}", arguments):
                    payload = await _invoke_tool_with_budget(
                        services.tool_invoker,
                        tool,
                        spec,
                        arguments,
                        config,
                        contract,
                        inspected,
                        changed,
                        phase == "repair" and not repair_inspected_since_failure,
                    )
        is_verification = (
            not skipped_after_error
            and verification_rejection is None
            and _is_verification_call(name, arguments, contract)
        )
        time_budget_exceeded = payload.get("failure_kind") == "time_budget_exceeded"
        if is_verification and not time_budget_exceeded:
            if _exit_code(payload) is None:
                payload = _verification_protocol_error_payload(payload)
        payload = cast(
            dict[str, object],
            sanitize_for_model_trace_and_persistence(
                payload, max_chars=services.max_tool_output_chars
            ),
        )
        tool_messages.append(
            cast(
                AnyMessage,
                ToolMessage(
                    content=json.dumps(payload, ensure_ascii=False, default=str),
                    tool_call_id=tool_call_id,
                    name=name,
                    status="error" if bool(payload.get("is_error")) else "success",
                ),
            )
        )
        event = _tool_event(name, arguments, payload)
        tool_events.append(event)
        if services.observability is not None:
            services.observability.record_tool_event(event)
            if skipped_after_error:
                services.observability.record_decision("tool_batch_aborted", event)
            elif verification_rejection is not None:
                services.observability.record_decision("verification_rejected", event)
        if skipped_after_error:
            continue
        if time_budget_exceeded:
            last_error = str(payload.get("content", "工具调用超过时间预算。"))
            repair_action_completed = False
            phase = "failed"
            batch_failed = True
            continue
        if bool(payload.get("is_error")):
            last_error = str(payload.get("content", "工具调用失败"))
            repair_action_completed = False
        if not bool(payload.get("is_error")) and name in _INSPECTION_TOOLS:
            path = arguments.get("path")
            if isinstance(path, str) and path not in inspected:
                inspected.append(path)
            if phase == "repair":
                repair_inspected_since_failure = True
        workspace_changes = _workspace_changes(payload)
        for path in workspace_changes:
            if path not in changed:
                changed.append(path)
        if not bool(payload.get("is_error")) and workspace_changes:
            workspace_revision += 1
            if phase == "repair":
                repair_action_completed = True
        if verification_rejection is not None:
            batch_failed = True
            phase = "repair"
            phase_history = _append_phase_name(phase_history, phase)
        elif is_verification:
            exit_code = _exit_code(payload)
            if exit_code is None:
                raise AssertionError("verification protocol errors must be normalized before state updates")
            verification_attempts.append(
                {
                    "command": str(arguments.get("command", "")),
                    "exit_code": exit_code,
                    "workspace_revision": workspace_revision,
                }
            )
            phase = "verify"
            phase_history = _append_phase_name(phase_history, phase)
            if bool(payload.get("is_error")) or exit_code != 0:
                last_error = str(payload.get("content", "验证失败"))
                repair_action_completed = False
                repair_inspected_since_failure = False
                phase = "repair"
                phase_history = _append_phase_name(phase_history, phase)
                batch_failed = True
            else:
                verified_workspace_revision = workspace_revision
                last_error = None
                repair_action_completed = False
        elif bool(payload.get("is_error")):
            phase = "repair"
            phase_history = _append_phase_name(phase_history, phase)
            batch_failed = True
        elif phase in {"plan", "inspect"}:
            phase = "implement"
            phase_history = _append_phase_name(phase_history, phase)
    update: AgentState = {
        "messages": tool_messages,
        "inspected_files": inspected,
        "changed_files": changed,
        "workspace_revision": workspace_revision,
        "verified_workspace_revision": verified_workspace_revision,
        "verification_attempts": verification_attempts,
        "repair_action_completed": repair_action_completed,
        "repair_inspected_since_failure": repair_inspected_since_failure,
        "tool_events": tool_events,
        "last_tool_error": last_error,
        "phase": phase,
        "phase_history": phase_history,
    }
    if phase == "failed":
        update["final_answer"] = last_error or "工具执行超过时间预算。"
    return update


async def trim_context(state: AgentState, config: RunnableConfig, services: GraphServices) -> AgentState:
    """Bound model context while preserving complete tool-call message groups."""
    del config
    messages = [
        cast(BaseMessage, message)
        for message in state.get("messages", [])
        if isinstance(message, BaseMessage)
    ]
    with _node_span(services, "trim_context", {"message_count": len(messages)}):
        removals, retained = trim_message_prefix(messages, services.max_context_messages)
        compacted_messages = [
            compacted
            for message in retained
            if isinstance(message, ToolMessage)
            if (compacted := _compact_tool_message(message, services.compact_tool_output_chars))
            is not None
        ]
        compacted_events = _compact_tool_events(
            state.get("tool_events", []), services.compact_tool_output_chars
        )
    if services.observability is not None and (
        removals or compacted_messages or compacted_events != state.get("tool_events", [])
    ):
        services.observability.record_decision(
            "context_trim",
            {
                "removed_messages": len(removals),
                "compacted_tool_messages": len(compacted_messages),
                "compacted_tool_events": len(compacted_events),
            },
        )
    update: AgentState = {}
    if removals or compacted_messages:
        update["messages"] = cast(list[AnyMessage], [*removals, *compacted_messages])
    if compacted_events != state.get("tool_events", []):
        update["tool_events"] = compacted_events
    return update


async def repair(state: AgentState, config: RunnableConfig, services: GraphServices) -> AgentState:
    del config
    with _node_span(services, "repair", {"last_tool_error": state.get("last_tool_error")}):
        attempts = int(state.get("repair_attempts", 0)) + 1
        if attempts > services.max_repair_attempts:
            return _failure_update(state, "repair_limit", "验证持续失败，已达到修复次数上限。")
        detail = state.get("last_tool_error") or "验证失败"
        return {
            "repair_attempts": attempts,
            "messages": [SystemMessage(content=f"上一轮验证失败：{detail}\n请检查失败原因后继续修复。")],
        }


async def summarize(state: AgentState, config: RunnableConfig, services: GraphServices) -> AgentState:
    del config
    with _node_span(services, "summarize"):
        message = _latest_ai_message(state.get("messages", []))
        if message is None or message.tool_calls:
            return _failure_update(state, "summary_protocol_error", "完成摘要不能包含新的工具调用。")
        final_answer = _message_content(message)
        return {
            "phase": "done",
            "final_answer": final_answer,
            "phase_history": _append_phases(state, "summarize", "done"),
        }


async def fail(state: AgentState, config: RunnableConfig, services: GraphServices) -> AgentState:
    del config
    with _node_span(services, "fail", {"last_tool_error": state.get("last_tool_error")}):
        return {
            "phase": "failed",
            "final_answer": state.get("final_answer") or state.get("last_tool_error") or "任务失败。",
            "phase_history": _append_phase(state, "failed"),
        }


def route_after_model(state: AgentState, services: GraphServices) -> str:
    if state.get("phase") == "failed":
        return "fail"
    message = _latest_ai_message(state.get("messages", []))
    if message is None:
        return "fail"
    if message.tool_calls:
        return "execute_tools"
    if state.get("phase") == "repair":
        return "action_required"
    contract = extract_task_contract(state.get("task", ""))
    if contract.requires_repository_inspection and (
        not state.get("inspected_files") or not state.get("changed_files")
    ):
        return "action_required"
    if _requires_successful_verification(contract) and not _has_successful_verification(
        state, contract
    ):
        return "action_required"
    return "summarize"


def route_after_tools(state: AgentState, services: GraphServices) -> str:
    del services
    phase = state.get("phase")
    if phase == "failed":
        return "fail"
    if phase == "repair":
        return "call_model" if state.get("repair_action_completed") else "repair"
    return "call_model"


def route_after_repair(state: AgentState) -> str:
    return "fail" if state.get("phase") == "failed" else "call_model"


def action_required(state: AgentState) -> AgentState:
    return _failure_update(
        state,
        "action_required",
        "仓库修复任务需要实际检查、修改和验证，不能只用文字宣布完成。",
    )


async def _await_model(
    model: Runnable,
    messages: Sequence[BaseMessage],
    config: RunnableConfig,
    remaining: float | None,
) -> object:
    call = model.ainvoke(list(messages), config=config)
    if remaining is None:
        return await call
    return await asyncio.wait_for(call, timeout=remaining)


async def _invoke_tool_with_budget(
    invoker: ContractAwareToolInvoker,
    tool: BaseTool,
    spec: ToolSpec,
    arguments: dict[str, object],
    config: RunnableConfig,
    contract: TaskContract,
    inspected_files: Sequence[str],
    changed_files: Sequence[str],
    verification_failed: bool,
) -> dict[str, object]:
    remaining = remaining_seconds_from_config(config)
    if remaining is not None and remaining <= 0:
        return _time_budget_payload(spec, arguments)
    task = asyncio.create_task(
        invoker.invoke(
            tool,
            spec,
            arguments,
            config,
            contract,
            inspected_files=inspected_files,
            changed_files=changed_files,
            verification_failed=verification_failed,
        )
    )
    try:
        if remaining is None:
            return await task
        return await asyncio.wait_for(task, timeout=remaining)
    except asyncio.TimeoutError:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return _time_budget_payload(spec, arguments)


def _failure_update(state: AgentState, kind: str, content: str) -> AgentState:
    safe_content = str(sanitize_for_model_trace_and_persistence(content))
    event = {"tool": "graph", "failure_kind": kind, "content": safe_content, "is_error": True}
    return {
        "phase": "failed",
        "last_tool_error": safe_content,
        "final_answer": safe_content,
        "tool_events": [*state.get("tool_events", []), event],
        "phase_history": _append_phase(state, "failed"),
    }


def _compact_tool_message(message: ToolMessage, max_chars: int) -> ToolMessage | None:
    content = _message_content(message)
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict) and isinstance(payload.get("content"), str):
        compacted = _compact_tool_output(payload["content"], max_chars)
        if compacted == payload["content"]:
            return None
        payload["content"] = compacted
        return cast(
            ToolMessage,
            message.model_copy(
                update={"content": json.dumps(payload, ensure_ascii=False, default=str)}
            ),
        )
    compacted = _compact_tool_output(content, max_chars)
    if compacted == content:
        return None
    return cast(ToolMessage, message.model_copy(update={"content": compacted}))


def _compact_tool_events(value: object, max_chars: int) -> list[dict[str, JSONValue]]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return []
    compacted_events: list[dict[str, JSONValue]] = []
    for raw_event in value:
        if not isinstance(raw_event, Mapping):
            continue
        event = dict(raw_event)
        content = event.get("content")
        if isinstance(content, str):
            event["content"] = _compact_tool_output(content, max_chars)
        compacted_events.append(cast(dict[str, JSONValue], _json_safe(event)))
    return compacted_events


def _compact_tool_output(content: str, max_chars: int) -> str:
    marker = "[已压缩工具输出"
    if len(content) <= max_chars or marker in content:
        return content
    return f"{content[:max_chars]}\n...[已压缩工具输出，原始长度={len(content)}]"


def _append_phase(state: AgentState, phase: str) -> list[str]:
    return _append_phase_name(list(state.get("phase_history", [])), phase)


def _append_phase_name(history: list[str], phase: str) -> list[str]:
    history = list(history)
    if not history or history[-1] != phase:
        history.append(phase)
    return history


def _append_phases(state: AgentState, *phases: str) -> list[str]:
    history = list(state.get("phase_history", []))
    for phase in phases:
        if not history or history[-1] != phase:
            history.append(phase)
    return history


def _latest_ai_message(messages: Sequence[BaseMessage]) -> AIMessage | None:
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            return message
    return None


def _latest_human_content(messages: Sequence[BaseMessage]) -> str:
    for message in reversed(messages):
        if message.type == "human":
            return _message_content(message)
    return ""


def _message_content(message: BaseMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, default=str)


def _tool_arguments(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, Mapping) else {}


def _unknown_tool_payload(name: str, arguments: Mapping[str, object]) -> dict[str, object]:
    return {
        "name": name,
        "arguments": dict(arguments),
        "content": f"unknown tool: {name}",
        "is_error": True,
        "failure_kind": "tool_protocol_error",
        "retryable": False,
        "metadata": {},
    }


def _tool_batch_aborted_payload(
    spec: ToolSpec | None, name: str, arguments: Mapping[str, object]
) -> dict[str, object]:
    return {
        "name": name,
        "arguments": dict(arguments),
        "content": "tool_batch_aborted: skipped because an earlier tool call in this batch failed",
        "is_error": True,
        "failure_kind": "tool_batch_aborted",
        "retryable": False,
        "permission": spec.required_permission.value if spec else None,
        "risk": spec.risk.value if spec else None,
        "metadata": {},
    }


def _verification_rejection(
    contract: TaskContract,
    name: str,
    arguments: Mapping[str, object],
    changed_files: Sequence[str],
) -> str | None:
    if name not in {"run_verification", "execute_command"}:
        return None
    command = arguments.get("command")
    if not isinstance(command, str):
        return "验证工具必须提供 command 参数。"
    if contract.expected_verification_command and not command_matches_required(
        command, contract.expected_verification_command
    ):
        return "必须运行任务声明的精确验证命令，不能替换为其他命令。"
    if _is_verification_call(name, arguments, contract) and not changed_files:
        return "必须先完成实际工作区修改，再运行验证。"
    return None


def _verification_rejection_payload(
    spec: ToolSpec | None,
    name: str,
    arguments: Mapping[str, object],
    content: str,
) -> dict[str, object]:
    return {
        "name": name,
        "arguments": dict(arguments),
        "content": f"verification_required: {content}",
        "is_error": True,
        "failure_kind": "verification_required",
        "retryable": False,
        "permission": spec.required_permission.value if spec else None,
        "risk": spec.risk.value if spec else None,
        "metadata": {},
    }


def _verification_protocol_error_payload(payload: Mapping[str, object]) -> dict[str, object]:
    normalized = dict(payload)
    content = str(payload.get("content", ""))
    normalized.update(
        {
            "content": (
                "verification_protocol_error: 验证工具必须返回包含 "
                f"'exit_code: <整数>' 的结果。原始结果：{content}"
            ),
            "is_error": True,
            "failure_kind": "verification_protocol_error",
            "retryable": False,
        }
    )
    return normalized


def _time_budget_payload(spec: ToolSpec, arguments: Mapping[str, object]) -> dict[str, object]:
    return {
        "name": spec.name,
        "arguments": dict(arguments),
        "content": "time_budget_exceeded: tool execution exceeded the remaining turn budget",
        "is_error": True,
        "failure_kind": "time_budget_exceeded",
        "retryable": False,
        "permission": spec.required_permission.value,
        "risk": spec.risk.value,
        "metadata": {},
    }


def _tool_event(
    name: str, arguments: Mapping[str, object], payload: Mapping[str, object]
) -> dict[str, JSONValue]:
    return {
        "tool": name,
        "arguments": _json_safe(arguments),
        "is_error": bool(payload.get("is_error")),
        "failure_kind": _json_safe(payload.get("failure_kind")),
        "content": _json_safe(payload.get("content")),
    }


def _workspace_changes(payload: Mapping[str, object]) -> list[str]:
    metadata = payload.get("metadata")
    if not isinstance(metadata, Mapping):
        return []
    changes = metadata.get("workspace_changes")
    if not isinstance(changes, Mapping):
        return []
    paths: list[str] = []
    for key in ("changed_files", "created_files", "removed_files"):
        value = changes.get(key)
        if isinstance(value, list):
            paths.extend(path for path in value if isinstance(path, str))
    return list(dict.fromkeys(paths))


def _is_verification_call(
    name: str, arguments: Mapping[str, object], contract: TaskContract
) -> bool:
    if name == "run_verification":
        return True
    command = arguments.get("command")
    return (
        name == "execute_command"
        and isinstance(command, str)
        and contract.expected_verification_command is not None
        and command_matches_required(command, contract.expected_verification_command)
    )


def _requires_successful_verification(contract: TaskContract) -> bool:
    return contract.requires_repository_inspection or contract.expected_verification_command is not None


def _has_successful_verification(state: AgentState, contract: TaskContract) -> bool:
    workspace_revision = state.get("workspace_revision")
    verified_workspace_revision = state.get("verified_workspace_revision")
    if (
        not isinstance(workspace_revision, int)
        or workspace_revision < 1
        or verified_workspace_revision != workspace_revision
    ):
        return False
    for attempt in reversed(state.get("verification_attempts", [])):
        command = attempt.get("command")
        exit_code = attempt.get("exit_code")
        if exit_code != 0 or not isinstance(command, str):
            continue
        if contract.expected_verification_command is None or command_matches_required(
            command, contract.expected_verification_command
        ):
            return True
    return False


def _exit_code(payload: Mapping[str, object]) -> int | None:
    if bool(payload.get("is_error")):
        return 1
    content = payload.get("content")
    if isinstance(content, str):
        match = _EXIT_CODE_PATTERN.search(content)
        if match:
            return int(match.group(1))
    return None


def _merge_usage(current: object, message: AIMessage) -> dict[str, int]:
    accumulator = UsageAccumulator(current if isinstance(current, Mapping) else None)
    accumulator.record(message)
    return accumulator.snapshot()


def _json_safe(value: object) -> JSONValue:
    return cast(JSONValue, json.loads(json.dumps(value, ensure_ascii=False, default=str)))


def _sanitize_ai_message(message: AIMessage) -> AIMessage:
    """Keep only a validated, redacted message before it reaches graph state."""
    payload = sanitize_for_model_trace_and_persistence(message.model_dump(mode="python"))
    if not isinstance(payload, Mapping):
        raise TypeError("sanitized AI message payload must be a mapping")
    return AIMessage.model_validate(dict(payload))


def _model_config(
    config: RunnableConfig, observability: GraphObservability | None
) -> RunnableConfig:
    if observability is None:
        return config
    model_config = dict(config)
    existing_metadata = config.get("metadata")
    metadata = dict(existing_metadata) if isinstance(existing_metadata, Mapping) else {}
    observability_metadata = observability.runnable_config()["metadata"]
    if isinstance(observability_metadata, Mapping):
        metadata.update(observability_metadata)
    model_config["metadata"] = metadata
    model_config["run_name"] = "insightagent-model"
    return cast(RunnableConfig, model_config)


def _messages_for_model(messages: Sequence[BaseMessage]) -> list[BaseMessage]:
    """Keep provider-required system context before the conversation transcript."""
    system_messages = [message for message in messages if isinstance(message, SystemMessage)]
    conversation_messages = [message for message in messages if not isinstance(message, SystemMessage)]
    if not system_messages:
        return conversation_messages
    system_context = "\n\n".join(_message_content(message) for message in system_messages)
    return [SystemMessage(content=system_context), *conversation_messages]


def _node_span(services: GraphServices, name: str, input_value: object | None = None):
    if services.observability is None:
        from contextlib import nullcontext

        return nullcontext()
    return services.observability.node_span(name, input_value)
