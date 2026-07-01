"""Core V5.0 agent loop with runtime state."""

from __future__ import annotations

import ast
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

from .context import ContextManager
from .memory import SlidingWindowMemory
from ..api.messages import Message
from ..api.providers import ModelClient, ToolArgumentsParseError
from ..api.resilience import ToolCallExtractor, build_repair_prompt
from .repository_snapshot import build_repository_snapshot
from .session import Session, SessionStore
from .task_contracts import TaskContract, command_matches_required, extract_task_contract, is_test_file_path
from .task_state import TaskPhase, TaskState, mark_final_answer, phase_instruction, transition_after_tool
from ..runtime.command_validation import CommandKind, CommandValidator
from ..runtime.failure_classifier import FailureKind
from ..runtime.tool_context import ToolContext
from ..runtime.types import ToolExecutionResult, ToolPermission, ToolRisk
from ..tools import ToolRegistry
from ..telemetry.usage import UsageTracker


DEFAULT_SYSTEM_PROMPT = """You are InsightAgent V5.0, a coding agent runtime with sessions, usage tracking, grep search, and self-healing repair loops.
Use tools when needed. After writing or editing code, call `run_verification` to test/compile the project, and keep editing and re-verifying until it passes.
Be direct, and report tool errors clearly."""

TraceHandler = Callable[[dict[str, Any]], None]

REPOSITORY_INSPECTION_TOOLS = {
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
WORKSPACE_FILE_MUTATION_TOOLS = {"write_file", "edit_file"}
COMMAND_VERIFICATION_TOOLS = {"run_verification", "execute_command"}
COMMAND_VALIDATOR = CommandValidator()


def _is_verification_attempt(name: str, arguments: dict[str, Any], expected_command: str | None) -> bool:
    if name == "run_verification":
        return True
    if name != "execute_command":
        return False
    actual_command = str(arguments.get("command") or "")
    if expected_command and command_matches_required(actual_command, expected_command):
        return True
    return COMMAND_VALIDATOR.classify(actual_command).kind == CommandKind.TEST


def _duplicated_python_methods(context: ToolContext, arguments: dict[str, Any]) -> list[str]:
    raw_path = str(arguments.get("path") or "")
    if not raw_path.endswith(".py"):
        return []
    new_text = str(arguments.get("new") or "")
    old_text = str(arguments.get("old") or "")
    inserted_methods = _python_method_names(new_text) - _python_method_names(old_text)
    if not inserted_methods:
        return []
    try:
        path = context.resolve_workspace_path(raw_path)
        source = path.read_text(encoding="utf-8")
    except OSError:
        return []
    existing_methods = _python_method_names(source)
    return sorted(inserted_methods & existing_methods)


def _python_method_names(source: str) -> set[str]:
    names: set[str] = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        for match in re.finditer(r"(?m)^\s+def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", source):
            names.add(match.group(1))
        return names
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names.add(item.name)
    return names


def _adds_unrequested_optional_entrypoint(task: str, arguments: dict[str, Any]) -> bool:
    new_text = str(arguments.get("new") or arguments.get("content") or "")
    lowered_new = new_text.lower()
    adds_option = "@click.option" in lowered_new or ".add_argument(" in lowered_new
    if not adds_option:
        return False
    lowered_task = task.lower()
    option_requested = (
        " option" in lowered_task
        or " flag" in lowered_task
        or "command-line" in lowered_task
        or "cli option" in lowered_task
        or "新增参数" in task
        or "新增选项" in task
        or re.search(r"--[A-Za-z0-9][A-Za-z0-9_-]*", task) is not None
    )
    return not option_requested


def _task_requests_verification(task: str) -> bool:
    lowered = task.lower()
    return any(marker in lowered for marker in ("verify", "verification", "验证"))


def _creates_unrelated_new_file(context: ToolContext, arguments: dict[str, Any]) -> bool:
    raw_path = str(arguments.get("path") or "")
    if not raw_path:
        return False
    try:
        path = context.resolve_workspace_path(raw_path)
    except Exception:
        return False
    if path.exists():
        return False
    suffix = path.suffix.lower()
    if not suffix:
        return True
    existing_suffixes: set[str] = set()
    for candidate in context.workspace.rglob("*"):
        if len(existing_suffixes) >= 64:
            break
        if candidate.is_file() and candidate.suffix:
            existing_suffixes.add(candidate.suffix.lower())
    return bool(existing_suffixes) and suffix not in existing_suffixes


def _reads_failing_test_file(arguments: dict[str, Any], task_contract: TaskContract) -> bool:
    raw_path = str(arguments.get("path") or "").replace("\\", "/").strip("/")
    if not raw_path:
        return False
    return raw_path in task_contract.failing_test_files


def _destructive_python_rewrite_removed_symbols(context: ToolContext, arguments: dict[str, Any]) -> list[str]:
    raw_path = str(arguments.get("path") or "")
    if not raw_path.endswith(".py") or is_test_file_path(raw_path):
        return []
    new_text = str(arguments.get("content") or "")
    if not new_text.strip():
        return []
    try:
        path = context.resolve_workspace_path(raw_path)
    except Exception:
        return []
    if not path.is_file():
        return []
    try:
        old_text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    old_symbols = _python_structural_symbols(old_text)
    if len(old_symbols) < 6:
        return []
    new_symbols = _python_structural_symbols(new_text)
    removed = sorted(old_symbols - new_symbols)
    if len(removed) < 3:
        return []
    old_lines = max(1, len(old_text.splitlines()))
    new_lines = len(new_text.splitlines())
    old_size = max(1, len(old_text))
    new_size = len(new_text)
    if len(new_symbols) <= int(len(old_symbols) * 0.7):
        return removed
    if new_lines <= int(old_lines * 0.75):
        return removed
    if new_size <= int(old_size * 0.75):
        return removed
    return []


def _python_structural_symbols(source: str) -> set[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set(re.findall(r"(?m)^\s*(?:class|def)\s+([A-Za-z_][A-Za-z0-9_]*)\b", source))
    symbols: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            symbols.add(node.name)
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    symbols.add(f"{node.name}.{item.name}")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.add(node.name)
    return symbols


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
    ) -> None:
        self.model_client = model_client
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
        task_contract = extract_task_contract(user_input)
        if task_contract.requires_repository_inspection and self.task_state.max_repairs == TaskState().max_repairs:
            self.task_state.max_repairs = 8
        repository_inspected = False
        failing_tests_inspected = not task_contract.failing_test_files
        workspace_file_mutated = False
        existing_non_test_file_modified = False
        post_failure_inspection_required = False
        self.messages.append(Message(role="user", content=user_input))
        self._sync_session()
        self._emit(trace, {"type": "user_message", "content": user_input})
        if task_contract.expected_verification_command or task_contract.requires_repository_inspection:
            self._emit(
                trace,
                {
                    "type": "task_contract_detected",
                    "expected_verification_command": task_contract.expected_verification_command,
                    "requires_repository_inspection": task_contract.requires_repository_inspection,
                    "failing_test_files": list(task_contract.failing_test_files),
                },
            )
        if task_contract.requires_repository_inspection:
            snapshot = build_repository_snapshot(self.tools.context.workspace)
            self.messages.append(Message(role="user", content=snapshot.content))
            self._sync_session()
            self._emit(
                trace,
                {
                    "type": "repository_snapshot_injected",
                    "file_count": snapshot.file_count,
                    "omitted_count": snapshot.omitted_count,
                },
            )
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
            if self.task_state.phase == TaskPhase.FAILED:
                return self._finalize_task_failed(iteration, trace)
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
                        allow_codeblock_write=self.task_state.phase != TaskPhase.SUMMARIZE,
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
                    requires_explicit_verification = bool(
                        task_contract.expected_verification_command
                        or task_contract.requires_repository_inspection
                        or _task_requests_verification(self.current_task)
                    )
                    needs_verification = requires_explicit_verification and self.task_state.phase in {
                        TaskPhase.IMPLEMENT,
                        TaskPhase.REPAIR,
                        TaskPhase.VERIFY,
                    }
                    contract_needs_patch = (
                        task_contract.requires_existing_non_test_patch
                        and not existing_non_test_file_modified
                    )
                    if self.resilience_enabled:
                        should_nudge = (
                            self.require_tool_use
                            and not failed_phase
                            and (not saw_tool_call or pending_repair or needs_verification or contract_needs_patch)
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
                execution_result = self._enforce_task_contract(
                    tool_call.name,
                    tool_call.arguments,
                    task_contract,
                    repository_inspected=repository_inspected,
                    failing_tests_inspected=failing_tests_inspected,
                    workspace_file_mutated=workspace_file_mutated,
                    existing_non_test_file_modified=existing_non_test_file_modified,
                    post_failure_inspection_required=post_failure_inspection_required,
                )
                existing_non_test_mutation = self._is_existing_non_test_file_mutation(
                    tool_call.name,
                    tool_call.arguments,
                )
                if execution_result is None:
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
                if tool_call.name in REPOSITORY_INSPECTION_TOOLS and not tool_result.is_error:
                    repository_inspected = True
                    post_failure_inspection_required = False
                if (
                    tool_call.name == "read_file"
                    and not tool_result.is_error
                    and _reads_failing_test_file(tool_call.arguments, task_contract)
                ):
                    failing_tests_inspected = True
                if tool_call.name in WORKSPACE_FILE_MUTATION_TOOLS and not tool_result.is_error:
                    workspace_file_mutated = True
                    existing_non_test_file_modified = (
                        existing_non_test_file_modified or existing_non_test_mutation
                    )
                transition_tool_name = tool_call.name
                if (
                    tool_call.name == "lsp_diagnostics"
                    and task_contract.expected_verification_command
                    and not tool_result.is_error
                ):
                    transition_tool_name = "read_file"
                self._transition_task_phase(transition_tool_name, tool_result.content, tool_result.is_error, trace)
                if (
                    tool_result.is_error
                    and _is_verification_attempt(
                        tool_call.name,
                        tool_call.arguments,
                        task_contract.expected_verification_command,
                    )
                ):
                    post_failure_inspection_required = True
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

    def _finalize_task_failed(self, iteration: int, trace: TraceHandler | None) -> AgentResult:
        last_error = self.task_state.last_error or "no recorded error"
        preview = last_error[:1500] + ("..." if len(last_error) > 1500 else "")
        warning = (
            "Stopped: task entered failed phase after reaching the repair limit. "
            "Partial progress is preserved in the workspace.\n"
            f"Last error:\n{preview}"
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
                "type": "task_failed",
                "iteration": iteration,
                "repair_attempts": self.task_state.repair_attempts,
                "max_repairs": self.task_state.max_repairs,
            },
        )
        self._emit(trace, {"type": "final_answer", "content": warning, "iterations": iteration})
        self._sync_session()
        return AgentResult(content=warning, messages=list(self.messages), iterations=iteration)

    def _model_complete(self, messages: list[Message], schemas: list[dict[str, Any]], tool_choice: Any | None):
        if tool_choice is None:
            return self.model_client.complete(messages, schemas)
        try:
            return self.model_client.complete(messages, schemas, tool_choice=tool_choice)
        except TypeError:
            # Model client predates the tool_choice parameter; fall back to auto.
            return self.model_client.complete(messages, schemas)

    def _forced_tool_for_phase(self) -> str | None:
        available = {tool["name"] for tool in self.tools.schemas()}
        if self.task_state.phase == TaskPhase.VERIFY:
            for name in ("run_verification", "execute_command"):
                if name in available:
                    return name
        if self.task_state.phase == TaskPhase.REPAIR:
            for name in ("edit_file", "read_file", "grep_search", "run_verification", "execute_command"):
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
            "<tool_call>{\"name\": \"edit_file\", \"arguments\": {\"path\": \"main.py\", \"old\": \"...\", \"new\": \"...\"}}</tool_call>. "
            "For existing repository files, prefer targeted `edit_file`; use `write_file` only for new small files."
            + verification_reminder
        )

    def _enforce_task_contract(
        self,
        name: str,
        arguments: dict[str, Any],
        task_contract: TaskContract,
        *,
        repository_inspected: bool,
        failing_tests_inspected: bool,
        workspace_file_mutated: bool,
        existing_non_test_file_modified: bool,
        post_failure_inspection_required: bool,
    ) -> ToolExecutionResult | None:
        expected_command = task_contract.expected_verification_command
        is_verification_attempt = _is_verification_attempt(name, arguments, expected_command)
        if is_verification_attempt and expected_command:
            actual_command = str(arguments.get("command") or "")
            if not command_matches_required(actual_command, expected_command):
                return self._blocked_tool_result(
                    name,
                    arguments,
                    content=(
                        f"Tool contract violation: {name} does not match the required "
                        "verification command.\n"
                        f"Required command: {expected_command}\n"
                        "Call run_verification with exactly this command before finalizing."
                    ),
                    repair_guidance=(
                        "Use the exact verification command from the user task. Do not substitute a "
                        "demo command, a partial command, or a different test runner."
                    ),
                )
        if (
            is_verification_attempt
            and task_contract.requires_existing_non_test_patch
            and workspace_file_mutated
            and not existing_non_test_file_modified
        ):
            return self._blocked_tool_result(
                name,
                arguments,
                content=(
                    "Tool contract violation: Modify at least one existing non-test repository file "
                    "before verifying a repository repair. Added standalone files are not enough for "
                    "this task."
                ),
                repair_guidance=(
                    "Inspect the existing implementation and patch the non-test source or configuration "
                    "that causes the failing behavior, then run the required verification command."
                ),
            )
        if (
            task_contract.requires_repository_inspection
            and name in WORKSPACE_FILE_MUTATION_TOOLS
            and not repository_inspected
        ):
            return self._blocked_tool_result(
                name,
                arguments,
                content=(
                    "Tool contract violation: Inspect the existing repository before modifying files. "
                    "Use read_file, grep_search, glob_search, git_status, or git_diff to identify the "
                    "relevant existing source and tests, then edit the repository files."
                ),
                repair_guidance=(
                    "Inspect the repository first. Prefer reading relevant existing files and tests over "
                    "creating standalone demo files."
                ),
            )
        if (
            task_contract.requires_repository_inspection
            and task_contract.failing_test_files
            and name in WORKSPACE_FILE_MUTATION_TOOLS
            and not failing_tests_inspected
        ):
            examples = ", ".join(task_contract.failing_test_files[:3])
            return self._blocked_tool_result(
                name,
                arguments,
                content=(
                    "Tool contract violation: Read the fail-to-pass test body before modifying source. "
                    f"Use read_file on the relevant test file first: {examples}. Grep results or test names "
                    "alone are not enough to identify the exact exercised call path."
                ),
                repair_guidance=(
                    "Call read_file with start_line/max_lines around the failing test, then patch the source "
                    "function or constructor directly exercised by that test line."
                ),
            )
        if (
            task_contract.requires_existing_non_test_patch
            and name == "write_file"
            and _creates_unrelated_new_file(self.tools.context, arguments)
        ):
            return self._blocked_tool_result(
                name,
                arguments,
                content=(
                    "Tool contract violation: Do not create unrelated new files with extensions that do "
                    "not already exist in this repository repair. Patch the existing source file imported "
                    "by the failing tests instead."
                ),
                repair_guidance=(
                    "Use grep_search/read_file to locate the existing implementation and edit that file. "
                    "Do not create standalone scratch or inferred files for a repository repair."
                ),
            )
        if task_contract.requires_existing_non_test_patch and name == "write_file":
            removed_symbols = _destructive_python_rewrite_removed_symbols(self.tools.context, arguments)
            if removed_symbols:
                preview = ", ".join(removed_symbols[:5])
                suffix = "..." if len(removed_symbols) > 5 else ""
                return self._blocked_tool_result(
                    name,
                    arguments,
                    content=(
                        "Tool contract violation: Refusing destructive Python source rewrite of an "
                        "existing repository file. The proposed write removes existing structure such as "
                        f"{preview}{suffix}. Use targeted edit_file around the relevant function instead."
                    ),
                    repair_guidance=(
                        "Do not replace a large existing Python source file with a partial reconstruction. "
                        "Read the relevant line range, then use edit_file with precise old/new text."
                    ),
                )
        if post_failure_inspection_required and name in WORKSPACE_FILE_MUTATION_TOOLS:
            return self._blocked_tool_result(
                name,
                arguments,
                content=(
                    "Tool contract violation: Inspect the latest failing verification before editing again. "
                    "Use read_file with relevant line ranges, grep_search, git_diff, parse_ast, or "
                    "get_function_signature to compare the failure with the current code and patch."
                ),
                repair_guidance=(
                    "Do not keep guessing after a failed verification. Inspect the current source, traceback, "
                    "or diff first, then make a targeted edit."
                ),
            )
        if name == "edit_file":
            if (
                task_contract.requires_existing_non_test_patch
                and _adds_unrequested_optional_entrypoint(self.current_task, arguments)
            ):
                return self._blocked_tool_result(
                    name,
                    arguments,
                    content=(
                        "Tool contract violation: Do not hide required fixes behind new optional flags, "
                        "commands, config switches, or alternate entrypoints unless the issue explicitly "
                        "asks for them. Patch the failing default path instead."
                    ),
                    repair_guidance=(
                        "Do not add a new CLI option as a workaround. Modify the existing behavior exercised "
                        "by the failing tests and then run the exact verification command."
                    ),
                )
            duplicated_methods = _duplicated_python_methods(self.tools.context, arguments)
            if duplicated_methods:
                methods = ", ".join(duplicated_methods)
                return self._blocked_tool_result(
                    name,
                    arguments,
                    content=(
                        f"Tool contract violation: {arguments.get('path')} already defines method(s): "
                        f"{methods}. Do not insert duplicate Python method definitions by editing the "
                        "class line or an unrelated snippet. Use parse_ast or get_function_signature to "
                        "locate the existing method, then edit that method body."
                    ),
                    repair_guidance=(
                        "Use parse_ast or get_function_signature to find the existing method line, then "
                        "use read_file with start_line/max_lines and edit the existing method body."
                    ),
                )
        if (
            task_contract.requires_existing_non_test_patch
            and name == "edit_file"
            and arguments.get("replace_all") is True
        ):
            old_text = str(arguments.get("old") or "")
            target_path = str(arguments.get("path") or "")
            if old_text and target_path and not is_test_file_path(target_path):
                try:
                    path = self.tools.context.resolve_workspace_path(target_path)
                    count = path.read_text(encoding="utf-8").count(old_text) if path.is_file() else 0
                except Exception:
                    count = 0
                if count > 1:
                    return self._blocked_tool_result(
                        name,
                        arguments,
                        content=(
                            "Tool contract violation: Avoid replace_all=True when the target snippet appears "
                            f"{count} times in an existing repository source file. This can corrupt adjacent "
                            "logic and cause repair loops. Make the old text unique with surrounding context "
                            "or rewrite the single relevant file intentionally."
                        ),
                        repair_guidance=(
                            "Do not use replace_all=True for repeated source snippets in repository repair "
                            "tasks. Read the file, include enough surrounding context to target one occurrence, "
                            "then use a single targeted edit_file replacement."
                        ),
                    )
        if task_contract.protects_test_files and name in WORKSPACE_FILE_MUTATION_TOOLS:
            path = str(arguments.get("path") or "")
            if is_test_file_path(path):
                return self._blocked_tool_result(
                    name,
                    arguments,
                    content=(
                        "Tool contract violation: Do not modify test files for this repository repair task. "
                        "Fix the existing source code instead, then run the required verification command."
                    ),
                    repair_guidance=(
                        "Revert the test-edit strategy. Inspect and modify the implementation files that make "
                        "the existing tests pass."
                    ),
                )
        return None

    def _is_existing_non_test_file_mutation(self, name: str, arguments: dict[str, Any]) -> bool:
        if name not in WORKSPACE_FILE_MUTATION_TOOLS:
            return False
        raw_path = str(arguments.get("path") or "")
        if not raw_path or is_test_file_path(raw_path):
            return False
        try:
            path = self.tools.context.resolve_workspace_path(raw_path)
        except Exception:
            return False
        return path.exists() and path.is_file()

    def _blocked_tool_result(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        content: str,
        repair_guidance: str,
    ) -> ToolExecutionResult:
        try:
            spec = self.tools.spec(name)
            permission = spec.required_permission
            risk = spec.risk
        except KeyError:
            permission = ToolPermission.READ
            risk = ToolRisk.LOW
        return ToolExecutionResult(
            name=name,
            arguments=arguments,
            content=content,
            is_error=True,
            failure_kind=FailureKind.TOOL_PROTOCOL_ERROR,
            retryable=True,
            repair_guidance=repair_guidance,
            permission=permission,
            risk=risk,
            suppressed=True,
            metadata={"blocked_by": "task_contract"},
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
