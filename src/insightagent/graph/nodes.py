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
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.runnables import Runnable, RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.types import interrupt

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
# A repair model needs enough context to locate the implementation, but an
# unbounded read loop is both expensive and prevents it from ever producing a
# patch.  Four successful inspections is sufficient for the task contract;
# after that every further read is rejected until the model edits the source.
_MAX_REPOSITORY_INSPECTIONS_WITHOUT_PATCH = 4
_MAX_TARGETED_INSPECTIONS_AFTER_BUDGET = 0
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
    max_repair_attempts: int = 5
    max_tool_output_chars: int = 8_000
    compact_tool_output_chars: int = 400
    max_context_messages: int = 16
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
    context_parts.append(
        f"当前工作区绝对路径是 {services.tool_context.workspace}。命令执行 cwd 已固定为该目录；"
        "不要使用 /workspace 等容器路径，命令和文件路径优先使用相对工作区路径。"
    )
    if contract.requires_repository_inspection:
        inspection_tools = "read_file、grep_search、glob_search"
        verification = contract.expected_verification_command or "任务声明的验证命令"
        context_parts.append(
            "仓库修复工具约束：源码检查只能使用 "
            f"{inspection_tools} 等只读工具，禁止用 execute_command 查看或打印源码；"
            "execute_command 和 run_verification 只能执行以下精确验证命令："
            f" {verification}。"
        )
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
    max_iterations = min(
        int(state.get("max_iterations", services.max_iterations)), services.max_iterations
    )
    repair_turn_allowed = (
        state.get("phase") == "repair"
        and int(state.get("repair_attempts", 0)) < services.max_repair_attempts
    )
    if iteration >= max_iterations and not repair_turn_allowed:
        return _failure_update(state, "iteration_limit", "达到工具循环上限，未能完成任务。")
    remaining = remaining_seconds_from_config(config)
    if remaining is not None and remaining <= 0:
        return _failure_update(state, "time_budget_exceeded", "回合时间预算已耗尽。")
    request_messages = _messages_for_model(state.get("messages", []))
    contract = extract_task_contract(state.get("task", ""))
    inspection_count = _inspection_count(state.get("tool_events", []))
    repair_force_implementation = (
        state.get("phase") == "repair"
        and bool(state.get("changed_files"))
        and _latest_verification_failed(state.get("verification_attempts", []))
        and not state.get("repair_action_completed", False)
    )
    force_implementation = (
        contract.requires_repository_inspection
        and not state.get("changed_files")
        and inspection_count >= _MAX_REPOSITORY_INSPECTIONS_WITHOUT_PATCH
    ) or repair_force_implementation
    required_tests_pending = bool(contract.failing_test_files) and not _required_tests_inspected(
        contract.failing_test_files, state.get("inspected_files", [])
    )
    latest_tool_failure = _latest_tool_failure_kind(state.get("tool_events", []))
    repair_needs_inspection = repair_force_implementation and (
        not state.get("repair_inspected_since_failure", False)
        or latest_tool_failure == "code_error"
    )
    implementation_needs_reinspection = (
        force_implementation
        and not state.get("changed_files")
        and latest_tool_failure == "code_error"
    )
    if force_implementation:
        # Repeated inspection requests make the transcript large and slow the
        # provider down. Keep only the task and the latest tool group when the
        # planner must transition into implementation.
        request_messages = _minimal_context_for_model(
            state.get("messages", []), task=state.get("task")
        )
        if required_tests_pending:
            required_tests = ", ".join(contract.failing_test_files)
            implementation_instruction = (
                "强制工作流状态：仓库检查预算已耗尽，但契约要求先读取失败测试。"
                f"下一次必须先调用 read_file，且只能读取指定测试文件：{required_tests}。"
                "这是预算后的唯一允许读取；在成功读取前禁止 edit_file/write_file，"
                "绝对不能修改测试文件。读取成功后下一轮立即修改现有实现文件。"
            )
        elif repair_needs_inspection or implementation_needs_reinspection:
            implementation_instruction = (
                "强制修复状态：上一次精确验证失败。下一次必须先读取一个相关的现有实现文件，"
                "以检查最新失败；只能使用 read_file 进行这一次定向复核，"
                "读取成功后下一轮必须修改实现文件。不要修改测试文件，也不要运行验证。"
            )
        elif repair_force_implementation:
            implementation_instruction = (
                "强制修复状态：上一次精确验证仍然失败，且已经完成有限的定向复核。"
                "下一次必须调用 edit_file 或 write_file 修改现有实现文件；"
                "不要再次读取、搜索或运行验证，也绝对不能修改测试文件。"
            )
        else:
            implementation_instruction = (
                "强制工作流状态：仓库检查预算已耗尽。你现在必须调用 edit_file 或 "
                "write_file 修改现有实现文件；不要再调用 read_file、grep_search、"
                "glob_search、parse_ast、get_function_signature 或 execute_command。"
            )
        request_messages = [
            SystemMessage(
                content=implementation_instruction
            ),
            *request_messages,
        ]
    _record_model_request(services, request_messages, fallback=False)
    context_recovered = False
    model = services.model
    if force_implementation:
        allow_repair_inspection = repair_needs_inspection or implementation_needs_reinspection
        if required_tests_pending:
            # The contract requires the fail-to-pass body before any mutation;
            # exposing only read_file makes that ordering provider-independent.
            allowed_tool_names = ("read_file",)
        elif allow_repair_inspection:
            allowed_tool_names = ("read_file", "edit_file", "write_file")
        else:
            allowed_tool_names = ("edit_file", "write_file")
        mutation_tools = [services._tools[name] for name in allowed_tool_names if name in services._tools]
        # The normal runner stores a ChatOpenAI instance inside a
        # RunnableBinding.  Bind the restricted tool set on the underlying
        # chat model; RunnableBinding itself has no bind_tools method.
        bind_target = getattr(model, "bound", model)
        bind_tools = getattr(bind_target, "bind_tools", None)
        if mutation_tools and callable(bind_tools):
            try:
                model = bind_tools(mutation_tools, tool_choice="required")
                if services.observability is not None:
                    services.observability.record_decision(
                        "implementation_tool_lock",
                        {
                            "requested_tools": [tool.name for tool in mutation_tools],
                            "model_type": type(model).__name__,
                            "bound_type": type(bind_target).__name__,
                            "status": "applied",
                        },
                    )
            except (TypeError, ValueError):
                # Some test/dummy runnables do not implement dynamic tool
                # choice; retain the normal model path for those callers.
                model = services.model
                if services.observability is not None:
                    services.observability.record_decision(
                        "implementation_tool_lock",
                        {
                            "requested_tools": [tool.name for tool in mutation_tools],
                            "model_type": type(bind_target).__name__,
                            "status": "unsupported",
                        },
                    )
    try:
        with _node_span(services, "call_model"):
            response = await _await_model(
                model,
                request_messages,
                _model_config(config, services.observability),
                remaining,
            )
    except asyncio.TimeoutError:
        return _failure_update(state, "time_budget_exceeded", "模型调用超过回合时间预算。")
    except Exception as error:
        if not _is_context_error(error):
            return _failure_update(state, "model_error", f"{type(error).__name__}: {error}")
        fallback_messages = _minimal_context_for_model(
            state.get("messages", []), task=state.get("task")
        )
        _record_model_request(services, fallback_messages, fallback=True)
        if services.observability is not None:
            services.observability.record_decision(
                "model_context_recovery",
                {
                    "original_message_count": len(request_messages),
                    "fallback_message_count": len(fallback_messages),
                    "failure_kind": "provider_context_error",
                },
            )
        try:
            with _node_span(services, "call_model_context_recovery"):
                response = await _await_model(
                    model,
                    fallback_messages,
                    _model_config(config, services.observability),
                    remaining,
                )
            context_recovered = True
        except asyncio.TimeoutError:
            return _failure_update(state, "time_budget_exceeded", "模型上下文恢复调用超过回合时间预算。")
        except Exception as fallback_error:
            return _failure_update(
                state,
                "model_error",
                f"{type(fallback_error).__name__}: {fallback_error} (context recovery failed)",
            )
    if not isinstance(response, AIMessage):
        response = AIMessage(content=str(response))
    response = _sanitize_ai_message(response)
    update: AgentState = {
        "messages": [response],
        "iteration": iteration + 1,
        "usage": _merge_usage(state.get("usage", {}), response),
    }
    if context_recovered:
        update["messages"] = [
            *_context_reset_removals(state.get("messages", [])),
            response,
        ]
    return update


async def execute_tools(state: AgentState, config: RunnableConfig, services: GraphServices) -> AgentState:
    message = _latest_ai_message(state.get("messages", []))
    if message is None:
        return _failure_update(state, "tool_protocol_error", "模型未返回可执行的工具调用。")
    contract = extract_task_contract(state.get("task", ""))
    approval_candidates = _approval_candidates(message, services, contract, state)
    if approval_candidates:
        decision = interrupt(
            {
                "type": "tool_approval",
                "workspace": str(services.tool_context.workspace),
                "tools": approval_candidates,
                "message": "高风险工具调用需要明确批准后才会执行。",
            }
        )
        if not _approval_granted(decision):
            return _approval_denied_update(state, message, services, decision)
        if services.observability is not None:
            services.observability.record_decision(
                "tool_approval",
                {"approved": True, "tool_count": len(approval_candidates)},
            )
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
    verification_called_this_batch = False
    workspace_changed_this_batch = False
    inspection_budget_feedback = False
    implementation_feedback: list[str] = []
    protected_test_feedback = False

    for tool_call in message.tool_calls:
        name = str(tool_call.get("name", ""))
        arguments = _tool_arguments(tool_call.get("args", {}))
        tool_call_id = str(tool_call.get("id", name))
        tool = services._tools.get(name)
        spec = services._tool_specs.get(name)
        redirected_test_read_from: str | None = None
        skipped_after_error = batch_failed
        verification_rejection: str | None = None
        inspection_budget_exhausted = _inspection_budget_exhausted(
            contract, tool_events, changed
        )
        if (
            inspection_budget_exhausted
            and name == "read_file"
            and contract.failing_test_files
            and not _required_tests_inspected(contract.failing_test_files, inspected)
        ):
            requested_path = str(arguments.get("path", "")).replace("\\", "/")
            required_path = contract.failing_test_files[0].replace("\\", "/")
            if requested_path != required_path:
                redirected_test_read_from = requested_path
                arguments = {**arguments, "path": required_path}
        allow_source_reinspection = (
            not changed
            and _latest_tool_failure_kind(tool_events) == "code_error"
            and _is_targeted_source_inspection(name, arguments)
        )
        if skipped_after_error:
            payload = _tool_batch_aborted_payload(spec, name, arguments)
        elif (
            inspection_budget_exhausted
            and name in _INSPECTION_TOOLS
            and not _is_required_test_inspection(
                contract,
                name,
                arguments,
                inspected,
                allow_after_failure=phase == "repair" and not repair_inspected_since_failure,
            )
            and (
                not _is_targeted_source_inspection(name, arguments)
                or _inspection_count(tool_events)
                >= _MAX_REPOSITORY_INSPECTIONS_WITHOUT_PATCH
                + _MAX_TARGETED_INSPECTIONS_AFTER_BUDGET
            )
            and not allow_source_reinspection
        ):
            payload = _inspection_budget_payload(name, arguments)
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
                        phase == "repair"
                        and not repair_inspected_since_failure
                        and _latest_verification_failed(verification_attempts),
                    )
        is_verification = (
            not skipped_after_error
            and verification_rejection is None
            and _is_verification_call(name, arguments, contract)
        )
        time_budget_exceeded = payload.get("failure_kind") == "time_budget_exceeded"
        if is_verification and not time_budget_exceeded:
            verification_called_this_batch = True
            if _exit_code(payload) is None:
                payload = _verification_protocol_error_payload(payload)
        payload = cast(
            dict[str, object],
            sanitize_for_model_trace_and_persistence(
                payload, max_chars=services.max_tool_output_chars
            ),
        )
        if redirected_test_read_from is not None and not payload.get("is_error"):
            metadata = payload.get("metadata")
            metadata_dict = dict(metadata) if isinstance(metadata, Mapping) else {}
            metadata_dict["redirected_from"] = redirected_test_read_from
            payload["metadata"] = metadata_dict
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
        event = _tool_event(name, arguments, payload, spec=spec, tool_call_id=tool_call_id)
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
            if payload.get("failure_kind") == "inspection_budget_exceeded":
                inspection_budget_feedback = True
            elif payload.get("failure_kind") == "code_error" and name in {"edit_file", "write_file"}:
                implementation_feedback.append(
                    "上一次实现工具调用没有产生有效的新补丁。下一次必须对现有实现做有意义的语义修改；"
                    "edit_file 的 old/new 文本必须不同，不能添加注释、空白或提交 no-op。"
                )
            elif (
                payload.get("failure_kind") == "task_contract"
                and name in {"edit_file", "write_file"}
                and _looks_like_test_path(str(arguments.get("path", "")))
            ):
                protected_test_feedback = True
            # Inspection-budget feedback is a recoverable planning nudge, not
            # a failed repair attempt.  Keeping the current phase prevents a
            # model that over-inspected from burning the finite repair budget
            # before it has made its first source edit.
            if payload.get("failure_kind") != "inspection_budget_exceeded":
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
            workspace_changed_this_batch = True
            workspace_revision += 1
            if phase == "repair":
                repair_action_completed = True
        if verification_rejection is not None:
            batch_failed = True
            if contract.requires_repository_inspection:
                # A repository-repair policy mismatch is recoverable model feedback,
                # not a failed patch. Keep the current phase so the correction does
                # not consume the finite repair budget.
                pass
            else:
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
        elif bool(payload.get("is_error")) and payload.get("failure_kind") != "inspection_budget_exceeded":
            phase = "repair"
            phase_history = _append_phase_name(phase_history, phase)
            batch_failed = True
        elif phase in {"plan", "inspect"}:
            phase = "implement"
            phase_history = _append_phase_name(phase_history, phase)

    if inspection_budget_feedback:
        tool_messages.append(
            SystemMessage(
                content=(
                    "检查预算已用尽。下一次模型调用必须直接使用 edit_file 或 write_file "
                    "修改现有实现文件；禁止继续调用任何读取、搜索、解析或 shell 检查工具。"
                )
            )
        )
    if implementation_feedback:
        tool_messages.append(SystemMessage(content="\n".join(implementation_feedback)))
    if protected_test_feedback:
        candidates = [path for path in inspected if not _looks_like_test_path(str(path))]
        candidate_text = ", ".join(candidates[-5:]) or "已读取的现有实现文件"
        tool_messages.append(
            SystemMessage(
                content=(
                    "刚才的编辑目标是测试文件，已被保护策略拒绝。下一次必须编辑现有实现文件，"
                    f"优先从这些已检查的实现候选中选择：{candidate_text}；绝不能再次调用测试路径。"
                )
            )
        )

    # A model can spend the final iteration editing a correct patch and never
    # get another turn to issue the required verification call.  Once a source
    # revision is present and still unverified, run only the contract-declared
    # command as a deterministic runtime action.  This preserves the exact
    # verification contract while preventing a valid last-turn patch from
    # being reported as an iteration failure.
    if (
        not batch_failed
        and not verification_called_this_batch
        and phase != "failed"
        and contract.expected_verification_command
        and workspace_revision > 0
        and workspace_changed_this_batch
        and workspace_revision != verified_workspace_revision
        and not _has_verification_for_revision(verification_attempts, workspace_revision)
        and (
            (workspace_changed_this_batch and bool(contract.failing_test_files))
            or int(state.get("iteration", 0))
            >= min(int(state.get("max_iterations", services.max_iterations)), services.max_iterations)
        )
    ):
        verification_tool = services._tools.get("run_verification")
        verification_spec = services._tool_specs.get("run_verification")
        if verification_tool is not None and verification_spec is not None:
            verification_arguments = {"command": contract.expected_verification_command}
            verification_payload = await _invoke_tool_with_budget(
                services.tool_invoker,
                verification_tool,
                verification_spec,
                verification_arguments,
                config,
                contract,
                inspected,
                changed,
                _latest_verification_failed(verification_attempts),
            )
            if _exit_code(verification_payload) is None:
                verification_payload = _verification_protocol_error_payload(verification_payload)
            verification_payload = cast(
                dict[str, object],
                sanitize_for_model_trace_and_persistence(
                    verification_payload, max_chars=services.max_tool_output_chars
                ),
            )
            verification_tool_call_id = f"auto-verify-{workspace_revision}"
            tool_messages.append(
                ToolMessage(
                    content=json.dumps(verification_payload, ensure_ascii=False, default=str),
                    tool_call_id=verification_tool_call_id,
                    name="run_verification",
                    status="error" if bool(verification_payload.get("is_error")) else "success",
                )
            )
            verification_event = _tool_event(
                "run_verification",
                verification_arguments,
                verification_payload,
                spec=verification_spec,
                tool_call_id=verification_tool_call_id,
            )
            verification_event["metadata"] = {
                "auto": True,
                "reason": "iteration_limit",
            }
            tool_events.append(verification_event)
            if services.observability is not None:
                services.observability.record_tool_event(verification_event)
                services.observability.record_decision(
                    "auto_verification", {"workspace_revision": workspace_revision}
                )
            exit_code = _exit_code(verification_payload)
            if exit_code is None:
                raise AssertionError("verification protocol errors must be normalized before state updates")
            verification_attempts.append(
                {
                    "command": contract.expected_verification_command,
                    "exit_code": exit_code,
                    "workspace_revision": workspace_revision,
                    "auto": True,
                }
            )
            phase = "verify"
            phase_history = _append_phase_name(phase_history, phase)
            if bool(verification_payload.get("is_error")) or exit_code != 0:
                last_error = str(verification_payload.get("content", "验证失败"))
                repair_action_completed = False
                repair_inspected_since_failure = False
                phase = "repair"
                phase_history = _append_phase_name(phase_history, phase)
            else:
                verified_workspace_revision = workspace_revision
                last_error = None
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


def _approval_candidates(
    message: AIMessage,
    services: GraphServices,
    contract: TaskContract,
    state: AgentState,
) -> list[dict[str, object]]:
    """Build a sanitized approval payload before any side-effecting tool runs."""
    if services.tool_context.approval_mode != "interrupt":
        return []
    changed = state.get("changed_files", [])
    candidates: list[dict[str, object]] = []
    for raw_call in message.tool_calls:
        name = str(raw_call.get("name", ""))
        arguments = _tool_arguments(raw_call.get("args", {}))
        spec = services._tool_specs.get(name)
        if spec is None or _verification_rejection(contract, name, arguments, changed):
            continue
        if not _requires_human_approval(spec):
            continue
        candidates.append(
            {
                "tool_call_id": str(raw_call.get("id", name)),
                "tool": name,
                "arguments": cast(
                    dict[str, object],
                    sanitize_for_model_trace_and_persistence(arguments, max_chars=2_000),
                ),
                "permission": spec.required_permission.value,
                "risk": spec.risk.value,
                "mcp_server": spec.mcp_server,
            }
        )
    return candidates


def _requires_human_approval(spec: ToolSpec) -> bool:
    return spec.required_permission.value in {
        "workspace-write",
        "execute",
        "mcp",
        "external",
    } or spec.risk.value in {"high", "critical"}


def _approval_granted(value: object) -> bool:
    if value is True:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {"approve", "approved", "allow", "allowed", "yes", "y"}
    if isinstance(value, Mapping):
        return _approval_granted(value.get("approved"))
    return False


def _approval_denied_update(
    state: AgentState,
    message: AIMessage,
    services: GraphServices,
    decision: object,
) -> AgentState:
    """Return model-visible denials for every pending call without running tools."""
    tool_messages: list[AnyMessage] = []
    tool_events: list[dict[str, JSONValue]] = list(state.get("tool_events", []))
    content = "approval_denied: 用户未批准高风险工具调用。"
    if decision not in (False, None, "", "no", "n"):
        content = "approval_denied: 审批结果无法识别，已按拒绝处理。"
    for raw_call in message.tool_calls:
        name = str(raw_call.get("name", ""))
        arguments = _tool_arguments(raw_call.get("args", {}))
        spec = services._tool_specs.get(name)
        payload = {
            "name": name,
            "arguments": arguments,
            "content": content,
            "is_error": True,
            "failure_kind": "approval_denied",
            "retryable": False,
            "permission": spec.required_permission.value if spec else None,
            "risk": spec.risk.value if spec else None,
            "metadata": {"approval_mode": services.tool_context.approval_mode},
        }
        tool_messages.append(
            ToolMessage(
                content=json.dumps(payload, ensure_ascii=False, default=str),
                tool_call_id=str(raw_call.get("id", name)),
                name=name,
                status="error",
            )
        )
        tool_events.append(
            _tool_event(name, arguments, payload, spec=spec, tool_call_id=str(raw_call.get("id", name)))
        )
    if services.observability is not None:
        services.observability.record_decision(
            "tool_approval",
            {"approved": False, "tool_count": len(message.tool_calls)},
        )
    return {
        "messages": tool_messages,
        "tool_events": tool_events,
        "last_tool_error": content,
        "phase": "repair",
        "phase_history": _append_phase_name(list(state.get("phase_history", [])), "repair"),
        "repair_action_completed": False,
    }


async def trim_context(state: AgentState, config: RunnableConfig, services: GraphServices) -> AgentState:
    """Bound model context while preserving complete tool-call message groups."""
    del config
    messages = [
        cast(BaseMessage, message)
        for message in state.get("messages", [])
        if isinstance(message, BaseMessage)
    ]
    with _node_span(services, "trim_context", {"message_count": len(messages)}):
        removals, retained = trim_message_prefix(
            messages,
            services.max_context_messages,
            preserve_context_anchors=True,
        )
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
        attempts = int(state.get("repair_attempts", 0))
        # A failed verification starts a repair budget, but protocol feedback
        # inside that repair (for example "inspect before edit", an exhausted
        # read budget, or an edit replacement mismatch) must not consume a
        # validation attempt. Otherwise a model that follows the contract can
        # be stopped before it gets a second edit/verification cycle.
        if not _is_repair_protocol_feedback(state):
            attempts += 1
        if attempts > services.max_repair_attempts:
            return _failure_update(state, "repair_limit", "验证持续失败，已达到修复次数上限。")
        detail = state.get("last_tool_error") or "验证失败"
        return {
            "repair_attempts": attempts,
            "messages": [SystemMessage(content=f"上一轮验证失败：{detail}\n请检查失败原因后继续修复。")],
        }


def _is_repair_protocol_feedback(state: AgentState) -> bool:
    """Return whether the latest failure is recoverable guidance, not a test failure."""
    protocol_kinds = {
        "task_contract",
        "inspection_budget_exceeded",
        "verification_required",
        "code_error",
        "invalid_arguments",
    }
    for raw_event in reversed(state.get("tool_events", [])):
        if not isinstance(raw_event, Mapping) or not raw_event.get("is_error"):
            continue
        return str(raw_event.get("failure_kind", "")) in protocol_kinds
    return False


async def summarize(state: AgentState, config: RunnableConfig, services: GraphServices) -> AgentState:
    del config
    with _node_span(services, "summarize"):
        message = _latest_ai_message(state.get("messages", []))
        if message is None or message.tool_calls:
            contract = extract_task_contract(state.get("task", ""))
            if message is not None and _has_successful_verification(state, contract):
                final_answer = _successful_verification_summary(state)
            else:
                return _failure_update(
                    state, "summary_protocol_error", "完成摘要不能包含新的工具调用。"
                )
        else:
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
    phase = state.get("phase")
    if phase == "failed":
        return "fail"
    if phase == "repair":
        return "call_model" if state.get("repair_action_completed") else "repair"
    if phase == "verify":
        contract = extract_task_contract(state.get("task", ""))
        latest = _latest_ai_message(state.get("messages", []))
        if _has_successful_verification(state, contract) and latest is not None:
            tool_calls = latest.tool_calls
            has_verification_call = any(
                _is_verification_call(
                    str(call.get("name", "")),
                    _tool_arguments(call.get("args", {})),
                    contract,
                )
                for call in tool_calls
            )
            # Read-only calls after a successful verification cannot improve the
            # patch. Also converge when the verification consumed the final
            # model iteration, instead of turning a valid result into a limit
            # failure before the model can provide a final answer.
            iteration = int(state.get("iteration", 0))
            max_iterations = min(
                int(state.get("max_iterations", 0) or 0), services.max_iterations
            )
            if (tool_calls and not has_verification_call) or iteration >= max_iterations:
                return "summarize"
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


def _inspection_budget_exhausted(
    contract: TaskContract,
    tool_events: Sequence[Mapping[str, object]],
    changed_files: Sequence[str],
) -> bool:
    if not contract.requires_repository_inspection or changed_files:
        return False
    return _inspection_count(tool_events) >= _MAX_REPOSITORY_INSPECTIONS_WITHOUT_PATCH


def _inspection_budget_payload(
    name: str, arguments: Mapping[str, object]
) -> dict[str, object]:
    return {
        "name": name,
        "arguments": dict(arguments),
        "content": (
            "inspection_budget_exceeded: 已完成足够的仓库检查；下一步必须使用 "
            "edit_file 或 write_file 修改现有 src/ 实现文件。不要继续读取源码，也不要在修改前运行验证。"
        ),
        "is_error": True,
        "failure_kind": "inspection_budget_exceeded",
        "retryable": False,
        "permission": "read",
        "risk": "low",
        "metadata": {"max_inspections_without_patch": _MAX_REPOSITORY_INSPECTIONS_WITHOUT_PATCH},
    }


def _is_required_test_inspection(
    contract: TaskContract,
    name: str,
    arguments: Mapping[str, object],
    inspected_files: Sequence[str],
    *,
    allow_after_failure: bool = False,
) -> bool:
    """Allow the one test read required by the mutation contract after the budget."""
    if name != "read_file" or not contract.failing_test_files:
        return False
    path = str(arguments.get("path", "")).replace("\\", "/")
    required = {item.replace("\\", "/") for item in contract.failing_test_files}
    inspected = {str(item).replace("\\", "/") for item in inspected_files}
    return path in required and (allow_after_failure or path not in inspected)


def _required_tests_inspected(
    failing_tests: Sequence[str], inspected_files: Sequence[str]
) -> bool:
    inspected = {str(path).replace("\\", "/") for path in inspected_files}
    required = {str(path).replace("\\", "/") for path in failing_tests}
    return required.issubset(inspected)


def _is_targeted_source_inspection(name: str, arguments: Mapping[str, object]) -> bool:
    """Allow focused implementation reads while rejecting post-budget test rereads."""
    if name == "read_file":
        path = str(arguments.get("path", "")).replace("\\", "/")
        return bool(path) and not _looks_like_test_path(path) and path.endswith(
            (".py", ".pyi", ".pyx")
        )
    if name in {"parse_ast", "get_function_signature", "find_dependencies", "get_code_metrics"}:
        path = str(arguments.get("path", "")).replace("\\", "/")
        return bool(path) and not _looks_like_test_path(path)
    if name in {"grep_search", "glob_search"}:
        target = " ".join(
            str(arguments.get(key, "")).replace("\\", "/")
            for key in ("glob", "pattern")
        )
        return bool(arguments.get("glob")) and not _looks_like_test_path(target)
    return False


def _inspection_count(tool_events: Sequence[Mapping[str, object]]) -> int:
    return sum(
        1
        for event in tool_events
        if event.get("tool") in _INSPECTION_TOOLS
    )


def _latest_tool_failure_kind(tool_events: Sequence[Mapping[str, object]]) -> str | None:
    if not tool_events:
        return None
    event = tool_events[-1]
    if not event.get("is_error"):
        return None
    failure_kind = event.get("failure_kind")
    return str(failure_kind) if failure_kind else None


def _has_verification_for_revision(
    verification_attempts: Sequence[Mapping[str, object]], workspace_revision: int
) -> bool:
    return any(
        isinstance(attempt, Mapping)
        and attempt.get("workspace_revision") == workspace_revision
        for attempt in verification_attempts
    )


def _is_targeted_implementation_inspection(
    name: str, arguments: Mapping[str, object]
) -> bool:
    """Allow a concrete file/symbol read after broad inspection is exhausted."""
    if name == "read_file":
        path = str(arguments.get("path", "")).replace("\\", "/")
        return bool(path) and not _looks_like_test_path(path) and path.endswith((".py", ".pyi", ".pyx")) and (
            arguments.get("start_line") is not None or arguments.get("max_lines") is not None
        )
    if name in {"parse_ast", "get_function_signature", "find_dependencies", "get_code_metrics"}:
        path = str(arguments.get("path", "")).replace("\\", "/")
        return bool(path) and not _looks_like_test_path(path)
    if name in {"grep_search", "glob_search"}:
        target = " ".join(
            str(arguments.get(key, "")).replace("\\", "/")
            for key in ("glob", "pattern")
        )
        return bool(arguments.get("glob")) and any(
            marker in target
            for marker in ("src/", "flask/", "requests/", "pylint/", "django/", "sphinx/")
        ) or (
            bool(arguments.get("glob"))
            and str(arguments.get("pattern", "")).strip() not in {"", ".", "*"}
            and len(str(arguments.get("pattern", ""))) < 160
        )
    return False


def _looks_like_test_path(path: str) -> bool:
    parts = {part.lower() for part in path.split("/")}
    name = path.rsplit("/", 1)[-1].lower()
    return bool(parts & {"test", "tests", "testing"}) or name.startswith("test_") or name.endswith("_test.py")


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
    name: str,
    arguments: Mapping[str, object],
    payload: Mapping[str, object],
    *,
    spec: ToolSpec | None = None,
    tool_call_id: str | None = None,
) -> dict[str, JSONValue]:
    is_error = bool(payload.get("is_error"))
    failure_kind = _json_safe(payload.get("failure_kind"))
    outcome = (
        "denied"
        if failure_kind == "permission_denied"
        else "failed"
        if is_error
        else "succeeded"
    )
    permission = payload.get("permission")
    risk = payload.get("risk")
    if permission is None and spec is not None:
        permission = spec.required_permission.value
    if risk is None and spec is not None:
        risk = spec.risk.value
    event: dict[str, JSONValue] = {
        "event_version": 1,
        "tool": name,
        "arguments": _json_safe(arguments),
        "is_error": is_error,
        "outcome": outcome,
        "failure_kind": failure_kind,
        "permission": _json_safe(permission),
        "risk": _json_safe(risk),
        "command_kind": _json_safe(payload.get("command_kind")),
        "retryable": bool(payload.get("retryable")),
        "suppressed": bool(payload.get("suppressed")),
        "repeat_count": _json_safe(payload.get("repeat_count", 0)),
        "content": _json_safe(payload.get("content")),
    }
    if tool_call_id:
        event["tool_call_id"] = tool_call_id
    metadata = payload.get("metadata")
    if isinstance(metadata, Mapping):
        event["metadata"] = _json_safe(metadata)
    return event


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


def _latest_verification_failed(attempts: Sequence[Mapping[str, object]]) -> bool:
    """Return whether the latest recorded verification actually failed."""
    if not attempts:
        return False
    exit_code = attempts[-1].get("exit_code")
    return isinstance(exit_code, int) and exit_code != 0


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


def _successful_verification_summary(state: AgentState) -> str:
    attempts = state.get("verification_attempts", [])
    command = "指定验证命令"
    if attempts and isinstance(attempts[-1].get("command"), str):
        command = str(attempts[-1]["command"])
    changed_files = [path for path in state.get("changed_files", []) if isinstance(path, str)]
    files = "、".join(changed_files) if changed_files else "代码"
    return f"已修改 {files}，验证命令 `{command}` 已通过。"


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


def _minimal_context_for_model(
    messages: Sequence[BaseMessage], *, task: str | None = None
) -> list[BaseMessage]:
    """Build a provider-safe retry context from the task and latest tool group."""
    system_messages = [message for message in messages if isinstance(message, SystemMessage)]
    human_messages = [message for message in messages if isinstance(message, HumanMessage)]
    if not human_messages and task:
        human_messages = [HumanMessage(content=task)]
    latest_ai_index = max(
        (index for index, message in enumerate(messages) if isinstance(message, AIMessage)),
        default=-1,
    )
    tail: list[BaseMessage] = []
    if latest_ai_index >= 0:
        latest_ai = messages[latest_ai_index]
        if isinstance(latest_ai, AIMessage):
            tail.append(latest_ai)
        tail.extend(
            message
            for message in messages[latest_ai_index + 1 :]
            if isinstance(message, ToolMessage)
        )
    if not system_messages and not human_messages and not tail:
        return list(messages[-1:])
    # Repository snapshots can be several thousand characters and are useful for
    # the initial plan, but they are stale noise after a failed edit/verification.
    # Retain the beginning (task policy) and the newest feedback while keeping
    # the recovery request small enough for providers with strict context limits.
    system_context = "\n\n".join(_message_content(message) for message in system_messages)
    if len(system_context) > 6_000:
        system_context = (
            f"{system_context[:3_500]}\n...[旧系统上下文已省略]\n"
            f"{system_context[-2_500:]}"
        )
    compact_system = [SystemMessage(content=system_context)] if system_context else []
    return _messages_for_model([*compact_system, *human_messages[-1:], *tail])


def _context_reset_removals(messages: Sequence[BaseMessage]) -> list[RemoveMessage]:
    """Drop stale assistant/tool history after a provider context recovery."""
    return [
        RemoveMessage(id=message.id)
        for message in messages
        if isinstance(message, (AIMessage, ToolMessage)) and isinstance(message.id, str) and message.id
    ]


def _is_context_error(error: BaseException) -> bool:
    text = str(error).lower()
    markers = (
        "messages 参数非法",
        "messages参数非法",
        "invalid message",
        "context length",
        "maximum context",
        "too many tokens",
        "context window",
    )
    return any(marker in text for marker in markers)


def _record_model_request(
    services: GraphServices, messages: Sequence[BaseMessage], *, fallback: bool
) -> None:
    if services.observability is None:
        return
    services.observability.record_decision(
        "model_request",
        {
            "message_count": len(messages),
            "content_chars": sum(len(_message_content(message)) for message in messages),
            "tool_message_count": sum(isinstance(message, ToolMessage) for message in messages),
            "fallback": fallback,
        },
    )


def _node_span(services: GraphServices, name: str, input_value: object | None = None):
    if services.observability is None:
        from contextlib import nullcontext

        return nullcontext()
    return services.observability.node_span(name, input_value)
