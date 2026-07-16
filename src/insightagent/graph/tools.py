"""LangChain 工具适配及其不对模型暴露的执行策略。"""

from __future__ import annotations

import asyncio
import ast
import copy
import json
import math
import multiprocessing
import os
import signal
import stat
import subprocess
import time
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from pathlib import Path
from typing import cast

from langchain_core.tools import BaseTool, StructuredTool
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, ConfigDict

from insightagent.graph.retry import (
    BACKOFF_FAILURE_KINDS,
    PERMANENT_FAILURE_KINDS,
    RetryPolicy,
)
from insightagent.graph.contracts import ContractViolation, TaskContract
from insightagent.runtime.failure_classifier import FailureClassifier, FailureKind
from insightagent.runtime.permissions import PermissionEnforcer
from insightagent.runtime.tool_context import (
    PermissionDenied,
    ToolContext,
    WorkspaceViolation,
)
from insightagent.runtime.types import (
    ToolExecutionResult,
    ToolPermission,
    ToolRisk,
    ToolSpec,
)
from insightagent.tools.base import Tool
from insightagent.tools.code_analysis_tools import (
    FindDependenciesTool,
    GetCodeMetricsTool,
    GetFunctionSignatureTool,
    ParseAstTool,
)
from insightagent.tools.execution_tools import ExecuteCommandTool, RunVerificationTool
from insightagent.tools.file_tools import EditFileTool, ReadFileTool, WriteFileTool
from insightagent.tools.search_tools import GlobSearchTool, GrepSearchTool
from insightagent.tools.state_tools import (
    GitDiffTool,
    GitStatusTool,
    LspDiagnosticsTool,
    TodoWriteTool,
)


DUPLICATE_CALL_MESSAGE = "这个调用刚刚执行过，结果没有变化，请基于上面已有的结果继续推进，不要再重复调用相同的读取/查询。"
_MAX_RECENT_SUCCESS = 16
_PROCESS_TERMINATION_GRACE_SECONDS = 0.2
_SNAPSHOT_IGNORED_DIRECTORIES = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
}


class _StrictToolArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _NoArguments(_StrictToolArguments):
    model_config = ConfigDict(extra="forbid")


class _CommandArguments(_StrictToolArguments):
    model_config = ConfigDict(extra="forbid")

    command: str
    cwd: str | None = None
    timeout: int = 60


class _VerificationArguments(_StrictToolArguments):
    model_config = ConfigDict(extra="forbid")

    command: str | None = None
    cwd: str | None = None
    timeout: int = 120


class _ReadFileArguments(_StrictToolArguments):
    model_config = ConfigDict(extra="forbid")

    path: str
    start_line: int | None = None
    max_lines: int | None = None


class _WriteFileArguments(_StrictToolArguments):
    model_config = ConfigDict(extra="forbid")

    path: str
    content: str


class _EditFileArguments(_StrictToolArguments):
    model_config = ConfigDict(extra="forbid")

    path: str
    old: str
    new: str
    replace_all: bool = False


class _GrepSearchArguments(_StrictToolArguments):
    model_config = ConfigDict(extra="forbid")

    pattern: str
    glob: str | None = None
    max_results: int | None = None


class _GlobSearchArguments(_StrictToolArguments):
    model_config = ConfigDict(extra="forbid")

    pattern: str
    max_results: int | None = None


class _PathArguments(_StrictToolArguments):
    model_config = ConfigDict(extra="forbid")

    path: str


class _OptionalPathArguments(_StrictToolArguments):
    model_config = ConfigDict(extra="forbid")

    path: str | None = None


class _TodoItem(_StrictToolArguments):
    model_config = ConfigDict(extra="forbid")

    content: str
    status: str


class _TodoWriteArguments(_StrictToolArguments):
    model_config = ConfigDict(extra="forbid")

    todos: list[_TodoItem]


class _LspDiagnosticsArguments(_StrictToolArguments):
    model_config = ConfigDict(extra="forbid")

    path: str | None = None
    max_results: int | None = None


class _FunctionSignatureArguments(_StrictToolArguments):
    model_config = ConfigDict(extra="forbid")

    path: str
    function_name: str
    class_name: str | None = None


@dataclass(frozen=True)
class _WorkerOutcome:
    content: str
    error_type: str | None = None


def _tool_worker(
    tool: Tool,
    arguments: dict[str, object],
    result_sender: Connection,
    ready_sender: Connection,
) -> None:
    try:
        os.setsid()
    except OSError:
        ready_sender.send(False)
        ready_sender.close()
        result_sender.close()
        return
    ready_sender.send(True)
    ready_sender.close()
    try:
        result_sender.send(_WorkerOutcome(content=tool.run(arguments)))
    except BaseException as error:
        result_sender.send(
            _WorkerOutcome(
                content=f"{type(error).__name__}: {error}",
                error_type=type(error).__name__,
            )
        )
    finally:
        result_sender.close()


def _run_in_worker(
    tool: Tool,
    arguments: dict[str, object],
    remaining_seconds: float | None,
) -> _WorkerOutcome:
    context = multiprocessing.get_context("fork")
    result_receiver, result_sender = context.Pipe(duplex=False)
    ready_receiver, ready_sender = context.Pipe(duplex=False)
    process = context.Process(
        target=_tool_worker,
        args=(tool, arguments, result_sender, ready_sender),
    )
    started_at = time.monotonic()
    process.start()
    result_sender.close()
    ready_sender.close()
    process_group_ready = False
    try:
        process_group_ready = _wait_for_ready(
            process, ready_receiver, remaining_seconds, started_at
        )
        if not process_group_ready:
            _terminate_worker(process, process_group_ready=False)
            raise TimeoutError(
                "tool worker did not start before the turn budget expired"
            )
        remaining = _remaining_budget(remaining_seconds, started_at)
        if remaining is not None and remaining <= 0:
            _terminate_worker(process, process_group_ready=True)
            raise TimeoutError("tool worker exceeded the turn budget")
        if not _wait_for_result(process, result_receiver, remaining):
            _terminate_worker(process, process_group_ready=True)
            raise TimeoutError("tool worker exceeded the turn budget")
        try:
            outcome = cast(_WorkerOutcome, result_receiver.recv())
        except EOFError as error:
            _terminate_worker(process, process_group_ready=process_group_ready)
            raise TimeoutError(
                "tool worker exited without returning a result"
            ) from error
        process.join(_PROCESS_TERMINATION_GRACE_SECONDS)
        if process.is_alive():
            _terminate_worker(process, process_group_ready=True)
        return outcome
    finally:
        ready_receiver.close()
        result_receiver.close()
        if process.is_alive():
            _terminate_worker(process, process_group_ready=process_group_ready)


def _wait_for_ready(
    process: BaseProcess,
    receiver: Connection,
    remaining_seconds: float | None,
    started_at: float,
) -> bool:
    while process.is_alive():
        remaining = _remaining_budget(remaining_seconds, started_at)
        if remaining is not None and remaining <= 0:
            return False
        timeout = 0.05 if remaining is None else min(0.05, remaining)
        if receiver.poll(timeout):
            return bool(receiver.recv())
    return False


def _wait_for_result(
    process: BaseProcess,
    receiver: Connection,
    remaining_seconds: float | None,
) -> bool:
    started_at = time.monotonic()
    while process.is_alive():
        remaining = _remaining_budget(remaining_seconds, started_at)
        if remaining is not None and remaining <= 0:
            return False
        timeout = 0.05 if remaining is None else min(0.05, remaining)
        if receiver.poll(timeout):
            return True
    return receiver.poll(0)


def _remaining_budget(
    remaining_seconds: float | None, started_at: float
) -> float | None:
    if remaining_seconds is None:
        return None
    return remaining_seconds - (time.monotonic() - started_at)


def _terminate_worker(process: BaseProcess, *, process_group_ready: bool) -> None:
    if process.pid is None:
        return
    if process_group_ready:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    elif process.is_alive():
        process.terminate()
    process.join(_PROCESS_TERMINATION_GRACE_SECONDS)
    if process.is_alive():
        if process_group_ready:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process.is_alive():
            process.kill()
        process.join(_PROCESS_TERMINATION_GRACE_SECONDS)


class ToolRuntime:
    """保留领域工具执行策略，向图提供结构化结果。"""

    def __init__(self, context: ToolContext, tools: list[Tool] | None = None) -> None:
        self.context = context
        resolved_tools = _default_domain_tools(context) if tools is None else tools
        self._tools: dict[str, Tool] = {}
        self._specs: dict[str, ToolSpec] = {}
        self._failure_classifier = FailureClassifier()
        self._permission_enforcer = PermissionEnforcer()
        self._retry_policy = RetryPolicy()
        self._non_retryable_failures: dict[str, ToolExecutionResult] = {}
        self._recent_success: OrderedDict[str, bool] = OrderedDict()
        self._backoff_attempts: dict[str, int] = {}
        for tool in resolved_tools:
            if tool.name in self._tools:
                raise ValueError(f"duplicate tool name: {tool.name}")
            self._tools[tool.name] = tool
            self._specs[tool.name] = _spec_for_tool(tool)

    def specs(self) -> dict[str, ToolSpec]:
        return copy.deepcopy(self._specs)

    def invoke(
        self,
        name: str,
        arguments: dict[str, object],
        *,
        remaining_seconds: float | None,
    ) -> ToolExecutionResult:
        started_at = time.monotonic()
        if name not in self._tools:
            return self._unknown_tool_result(name, arguments)
        spec = self._specs[name]
        if remaining_seconds is not None and remaining_seconds <= 0:
            return _time_budget_result(name, arguments, spec=spec)
        signature = _tool_signature(name, arguments)
        duplicate = self._duplicate_read_result(name, arguments, spec, signature)
        if duplicate is not None:
            return duplicate
        suppressed_failure = self._suppressed_failure_result(
            name, arguments, spec, signature
        )
        if suppressed_failure is not None:
            return suppressed_failure
        permission = self._permission_enforcer.check(spec, self.context, arguments)
        if not permission.allowed:
            return self._permission_denied_result(
                name,
                arguments,
                spec,
                permission.reason,
                permission.command_kind,
                signature,
            )
        metadata: dict[str, object] = {"signature": signature}
        prior_attempts = self._backoff_attempts.get(signature, 0)
        if prior_attempts > 0:
            backoff_delay = self._retry_policy.backoff_delay(prior_attempts)
            current_remaining = _remaining_budget(remaining_seconds, started_at)
            if current_remaining is not None and backoff_delay >= current_remaining:
                metadata["backoff_delay"] = backoff_delay
                metadata["backoff_attempt"] = prior_attempts
                return _time_budget_result(
                    name, arguments, spec=spec, metadata=metadata
                )
            self._retry_policy.wait(prior_attempts)
            metadata["backoff_delay"] = backoff_delay
            metadata["backoff_attempt"] = prior_attempts
        execution_remaining = _remaining_budget(remaining_seconds, started_at)
        if execution_remaining is not None and execution_remaining <= 0:
            return _time_budget_result(name, arguments, spec=spec, metadata=metadata)
        result_arguments = _cap_timeout(arguments, execution_remaining)
        try:
            if spec.executes_code:
                outcome = _run_execution_tool(
                    self._tools[name], result_arguments, execution_remaining
                )
            else:
                outcome = _run_in_worker(
                    self._tools[name], result_arguments, execution_remaining
                )
        except subprocess.TimeoutExpired as error:
            if _execution_exhausted_turn_budget(result_arguments, execution_remaining):
                result = _time_budget_result(
                    name, result_arguments, spec=spec, metadata=metadata
                )
            else:
                result = self._result_from_outcome(
                    name,
                    result_arguments,
                    spec,
                    permission.command_kind,
                    metadata,
                    _WorkerOutcome(
                        content=f"TimeoutExpired: {error}",
                        error_type="TimeoutExpired",
                    ),
                )
        except TimeoutError:
            result = _time_budget_result(
                name, result_arguments, spec=spec, metadata=metadata
            )
        except Exception as error:
            result = self._result_from_outcome(
                name,
                result_arguments,
                spec,
                permission.command_kind,
                metadata,
                _WorkerOutcome(
                    content=f"{type(error).__name__}: {error}",
                    error_type=type(error).__name__,
                ),
            )
        else:
            result = self._result_from_outcome(
                name,
                result_arguments,
                spec,
                permission.command_kind,
                metadata,
                outcome,
            )
        self._record_outcome(signature, spec, result)
        return result

    def _unknown_tool_result(
        self, name: str, arguments: dict[str, object]
    ) -> ToolExecutionResult:
        content = f"KeyError: unknown tool: {name}"
        classification = self._failure_classifier.classify(name, content, is_error=True)
        return ToolExecutionResult(
            name=name,
            arguments=arguments,
            content=content,
            is_error=True,
            failure_kind=classification.kind,
            retryable=classification.retryable,
            repair_guidance=classification.repair_guidance,
        )

    def _duplicate_read_result(
        self,
        name: str,
        arguments: dict[str, object],
        spec: ToolSpec,
        signature: str,
    ) -> ToolExecutionResult | None:
        if (
            spec.required_permission != ToolPermission.READ
            or signature not in self._recent_success
        ):
            return None
        self._recent_success.move_to_end(signature)
        return ToolExecutionResult(
            name=name,
            arguments=arguments,
            content=DUPLICATE_CALL_MESSAGE,
            is_error=False,
            permission=spec.required_permission,
            risk=spec.risk,
            suppressed=True,
            metadata={"signature": signature, "duplicate": True},
        )

    def _suppressed_failure_result(
        self,
        name: str,
        arguments: dict[str, object],
        spec: ToolSpec,
        signature: str,
    ) -> ToolExecutionResult | None:
        prior_failure = self._non_retryable_failures.get(signature)
        if prior_failure is None:
            return None
        return ToolExecutionResult(
            name=name,
            arguments=arguments,
            content=(
                "Repeated non-retryable tool call suppressed by runtime harness. "
                f"failure_kind={_failure_value(prior_failure.failure_kind)}; "
                f"prior_result:\n{prior_failure.content}"
            ),
            is_error=True,
            failure_kind=prior_failure.failure_kind,
            retryable=False,
            repair_guidance=prior_failure.repair_guidance,
            permission=spec.required_permission,
            risk=spec.risk,
            command_kind=prior_failure.command_kind,
            suppressed=True,
            repeat_count=prior_failure.repeat_count + 1,
            metadata={"signature": signature},
        )

    def _permission_denied_result(
        self,
        name: str,
        arguments: dict[str, object],
        spec: ToolSpec,
        reason: str,
        command_kind: str | None,
        signature: str,
    ) -> ToolExecutionResult:
        content = f"PermissionDenied: {reason}"
        classification = self._failure_classifier.classify(name, content, is_error=True)
        return ToolExecutionResult(
            name=name,
            arguments=arguments,
            content=content,
            is_error=True,
            failure_kind=FailureKind.PERMISSION_DENIED,
            retryable=False,
            repair_guidance=classification.repair_guidance,
            permission=spec.required_permission,
            risk=spec.risk,
            command_kind=command_kind,
            metadata={"signature": signature},
        )

    def _result_from_outcome(
        self,
        name: str,
        arguments: dict[str, object],
        spec: ToolSpec,
        command_kind: str | None,
        metadata: dict[str, object],
        outcome: _WorkerOutcome,
    ) -> ToolExecutionResult:
        is_error = outcome.error_type is not None or _looks_like_error(
            name, outcome.content
        )
        classification = _classify_worker_outcome(
            self._failure_classifier, name, outcome, is_error
        )
        return ToolExecutionResult(
            name=name,
            arguments=arguments,
            content=outcome.content,
            is_error=is_error,
            failure_kind=classification.kind if is_error else None,
            retryable=classification.retryable,
            repair_guidance=classification.repair_guidance,
            permission=spec.required_permission,
            risk=spec.risk,
            command_kind=command_kind,
            metadata=metadata,
        )

    def _record_outcome(
        self, signature: str, spec: ToolSpec, result: ToolExecutionResult
    ) -> None:
        if spec.required_permission != ToolPermission.READ:
            self._recent_success.clear()
        if not result.is_error:
            if spec.required_permission == ToolPermission.READ:
                self._recent_success[signature] = True
                self._recent_success.move_to_end(signature)
                while len(self._recent_success) > _MAX_RECENT_SUCCESS:
                    self._recent_success.popitem(last=False)
            self._backoff_attempts.pop(signature, None)
            return
        if result.failure_kind in BACKOFF_FAILURE_KINDS:
            attempt = self._backoff_attempts.get(signature, 0) + 1
            self._backoff_attempts[signature] = attempt
            if attempt >= self._retry_policy.max_attempts:
                self._non_retryable_failures[signature] = result
        elif result.failure_kind in PERMANENT_FAILURE_KINDS:
            self._non_retryable_failures[signature] = result


def build_builtin_tools(
    context: ToolContext, runtime: ToolRuntime | None = None
) -> list[BaseTool]:
    resolved_runtime = runtime or ToolRuntime(context)
    schemas = _argument_models()
    tools: list[BaseTool] = []
    for name, spec in resolved_runtime.specs().items():
        args_schema = schemas.get(name)
        if args_schema is None:
            raise ValueError(f"missing explicit LangChain schema for tool: {name}")
        tools.append(
            StructuredTool(
                name=name,
                description=spec.description,
                args_schema=args_schema,
                func=_tool_handler(resolved_runtime, name),
            )
        )
    return tools


def _tool_handler(runtime: ToolRuntime, name: str):
    def handler(config: RunnableConfig, **arguments: object) -> dict[str, object]:
        return _result_payload(
            runtime.invoke(
                name,
                _json_compatible_arguments(arguments),
                remaining_seconds=_remaining_seconds_from_config(config),
            )
        )

    return handler


def _json_compatible_arguments(arguments: dict[str, object]) -> dict[str, object]:
    return cast(dict[str, object], _json_compatible_value(arguments))


def _json_compatible_value(value: object) -> object:
    if isinstance(value, BaseModel):
        return _json_compatible_value(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {str(key): _json_compatible_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_compatible_value(item) for item in value]
    return value


def _result_payload(result: ToolExecutionResult) -> dict[str, object]:
    return {
        "name": result.name,
        "arguments": result.arguments,
        "content": result.content,
        "is_error": result.is_error,
        "failure_kind": _failure_value(result.failure_kind) or None,
        "retryable": result.retryable,
        "permission": result.permission.value,
        "risk": result.risk.value,
        "command_kind": result.command_kind,
        "repair_guidance": result.repair_guidance,
        "suppressed": result.suppressed,
        "repeat_count": result.repeat_count,
        "metadata": result.metadata,
    }


def _argument_models() -> dict[str, type[BaseModel]]:
    return {
        "execute_command": _CommandArguments,
        "run_verification": _VerificationArguments,
        "read_file": _ReadFileArguments,
        "write_file": _WriteFileArguments,
        "edit_file": _EditFileArguments,
        "grep_search": _GrepSearchArguments,
        "glob_search": _GlobSearchArguments,
        "git_status": _NoArguments,
        "git_diff": _OptionalPathArguments,
        "todo_write": _TodoWriteArguments,
        "lsp_diagnostics": _LspDiagnosticsArguments,
        "parse_ast": _PathArguments,
        "get_function_signature": _FunctionSignatureArguments,
        "find_dependencies": _PathArguments,
        "get_code_metrics": _PathArguments,
    }


def _default_domain_tools(context: ToolContext) -> list[Tool]:
    tools = [
        ExecuteCommandTool(context),
        RunVerificationTool(context),
        ReadFileTool(context),
        WriteFileTool(context),
        EditFileTool(context),
        GrepSearchTool(context),
        GlobSearchTool(context),
        GitStatusTool(context),
        GitDiffTool(context),
        TodoWriteTool(context),
        LspDiagnosticsTool(context),
        ParseAstTool(context),
        GetFunctionSignatureTool(context),
        FindDependenciesTool(context),
        GetCodeMetricsTool(context),
    ]
    return cast(list[Tool], tools)


def _spec_for_tool(tool: Tool) -> ToolSpec:
    permission = ToolPermission.READ
    risk = ToolRisk.LOW
    mutates = False
    executes = False
    tags: tuple[str, ...] = ()
    if tool.name in {"write_file", "edit_file", "todo_write"}:
        permission = ToolPermission.WORKSPACE_WRITE
        risk = ToolRisk.MEDIUM
        mutates = True
        tags = ("workspace", "write")
    elif tool.name in {"execute_command", "run_verification"}:
        permission = ToolPermission.EXECUTE
        risk = ToolRisk.HIGH
        executes = True
        tags = ("shell", "verify" if tool.name == "run_verification" else "command")
    elif tool.name in {
        "read_file",
        "grep_search",
        "glob_search",
        "git_status",
        "git_diff",
    }:
        tags = ("workspace", "read")
    else:
        tags = ("analysis", "read")
    return ToolSpec(
        name=tool.name,
        description=tool.description,
        input_schema=copy.deepcopy(tool.input_schema),
        required_permission=permission,
        risk=risk,
        mutates_workspace=mutates,
        executes_code=executes,
        tags=tags,
    )


def _cap_timeout(
    arguments: dict[str, object], remaining_seconds: float | None
) -> dict[str, object]:
    capped = dict(arguments)
    if remaining_seconds is None or "timeout" not in capped:
        return capped
    requested = int(cast(int, capped["timeout"]))
    capped["timeout"] = min(requested, max(1, math.floor(remaining_seconds)))
    return capped


def _run_execution_tool(
    tool: Tool,
    arguments: dict[str, object],
    remaining_seconds: float | None,
) -> _WorkerOutcome:
    if not isinstance(tool, (ExecuteCommandTool, RunVerificationTool)):
        raise TypeError(
            f"execution spec has an unsupported tool type: {type(tool).__name__}"
        )
    return _WorkerOutcome(
        content=tool.run_with_turn_timeout(arguments, remaining_seconds)
    )


def _execution_exhausted_turn_budget(
    arguments: dict[str, object], remaining_seconds: float | None
) -> bool:
    if remaining_seconds is None:
        return False
    requested = arguments.get("timeout")
    return isinstance(requested, int) and remaining_seconds <= requested


def remaining_seconds_from_config(config: RunnableConfig) -> float | None:
    configurable = config.get("configurable")
    if not isinstance(configurable, dict):
        return None
    values: list[float] = []
    deadline = configurable.get("insightagent_deadline_monotonic")
    if isinstance(deadline, (float, int)) and not isinstance(deadline, bool):
        values.append(float(deadline) - time.monotonic())
    remaining = configurable.get("remaining_seconds")
    if isinstance(remaining, (float, int)) and not isinstance(remaining, bool):
        values.append(float(remaining))
    return min(values) if values else None


_remaining_seconds_from_config = remaining_seconds_from_config


def _classify_worker_outcome(
    classifier: FailureClassifier,
    name: str,
    outcome: _WorkerOutcome,
    is_error: bool,
):
    if outcome.error_type == "PermissionDenied":
        return classifier.classify_exception(PermissionDenied(outcome.content), name)
    if outcome.error_type == "WorkspaceViolation":
        return classifier.classify_exception(WorkspaceViolation(outcome.content), name)
    if outcome.error_type == "TimeoutExpired":
        return classifier.classify_exception(subprocess.TimeoutExpired(name, 0), name)
    return classifier.classify(name, outcome.content, is_error=is_error)


def _time_budget_result(
    name: str,
    arguments: dict[str, object],
    *,
    spec: ToolSpec | None = None,
    metadata: dict[str, object] | None = None,
) -> ToolExecutionResult:
    return ToolExecutionResult(
        name=name,
        arguments=arguments,
        content="time_budget_exceeded: tool execution exceeded the remaining turn budget",
        is_error=True,
        failure_kind="time_budget_exceeded",
        retryable=False,
        repair_guidance="The remaining turn budget expired. Narrow the next action or report the timeout.",
        permission=spec.required_permission if spec else ToolPermission.READ,
        risk=spec.risk if spec else ToolRisk.LOW,
        metadata=metadata or {},
    )


def _tool_signature(name: str, arguments: dict[str, object]) -> str:
    return (
        name
        + ":"
        + json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str)
    )


def _looks_like_error(name: str, content: str) -> bool:
    return name in {"execute_command", "run_verification"} and not content.startswith(
        "exit_code: 0\n"
    )


def _failure_value(value: object) -> str:
    return getattr(value, "value", str(value)) if value is not None else ""


@dataclass(frozen=True)
class _WorkspaceFile:
    content: bytes
    mode: int


@dataclass(frozen=True)
class WorkspaceChanges:
    """Workspace delta retained only for the current tool invocation."""

    changed_files: tuple[str, ...]
    created_files: tuple[str, ...]
    removed_files: tuple[str, ...]
    removed_symbols: dict[str, tuple[str, ...]]
    unsafe_paths: tuple[str, ...]

    def metadata(self) -> dict[str, object]:
        return {
            "changed_files": list(self.changed_files),
            "created_files": list(self.created_files),
            "removed_files": list(self.removed_files),
            "removed_symbols": {
                path: list(symbols) for path, symbols in self.removed_symbols.items()
            },
            "unsafe_paths": list(self.unsafe_paths),
        }


@dataclass(frozen=True)
class _WorkspaceSnapshot:
    root: Path
    files: dict[str, _WorkspaceFile]
    directories: frozenset[str]
    special_paths: frozenset[str]

    def changes(self) -> WorkspaceChanges:
        current_files, _, current_special_paths = _capture_workspace(self.root)
        before_paths = set(self.files)
        current_paths = set(current_files)
        created = tuple(sorted(current_paths - before_paths))
        removed = tuple(sorted(before_paths - current_paths))
        modified = {
            path
            for path in before_paths & current_paths
            if self.files[path] != current_files[path]
        }
        changed = tuple(sorted(modified | set(removed)))
        removed_symbols: dict[str, tuple[str, ...]] = {}
        for path in changed:
            if not path.endswith(".py"):
                continue
            before_symbols = _python_symbols(self.files[path].content)
            if not before_symbols:
                continue
            after = current_files.get(path)
            after_symbols = _python_symbols(after.content) if after is not None else set()
            deleted = tuple(sorted(before_symbols - after_symbols))
            threshold = max(3, math.ceil(len(before_symbols) * 0.4))
            if len(deleted) > threshold:
                removed_symbols[path] = deleted
        unsafe_paths = tuple(sorted(current_special_paths - self.special_paths))
        return WorkspaceChanges(changed, created, removed, removed_symbols, unsafe_paths)

    def restore(self) -> None:
        current_files, current_directories, current_special_paths = _capture_workspace(self.root)
        for path in sorted(set(current_files) - set(self.files), reverse=True):
            target = self.root / path
            if target.exists() or target.is_symlink():
                target.unlink()
        for path in sorted(current_special_paths - self.special_paths, reverse=True):
            target = self.root / path
            if target.exists() or target.is_symlink():
                target.unlink()
        for directory in sorted(
            current_directories - self.directories,
            key=lambda value: value.count("/"),
            reverse=True,
        ):
            target = self.root / directory
            try:
                target.rmdir()
            except OSError:
                continue
        for path, original in self.files.items():
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_symlink():
                target.unlink()
            elif target.exists() and not target.is_file():
                raise RuntimeError(f"cannot restore non-file workspace path: {path}")
            if target.exists():
                target.chmod(target.stat().st_mode | stat.S_IWUSR)
            target.write_bytes(original.content)
            target.chmod(original.mode)
        for directory in self.directories:
            (self.root / directory).mkdir(parents=True, exist_ok=True)


class WorkspaceSnapshotService:
    """Captures complete protected-workspace state without persisting it."""

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    def capture(self) -> _WorkspaceSnapshot:
        files, directories, special_paths = _capture_workspace(self._context.workspace)
        return _WorkspaceSnapshot(
            self._context.workspace, files, frozenset(directories), frozenset(special_paths)
        )


class ContractAwareToolInvoker:
    """The only graph-level entry point for built-in and MCP BaseTool calls."""

    def __init__(
        self,
        tool_context: ToolContext,
        snapshot_service: WorkspaceSnapshotService | None = None,
    ) -> None:
        self._tool_context = tool_context
        self._snapshots = snapshot_service or WorkspaceSnapshotService(tool_context)

    async def invoke(
        self,
        tool: BaseTool,
        spec: ToolSpec,
        arguments: dict[str, object],
        config: RunnableConfig,
        contract: TaskContract,
        *,
        inspected_files: Sequence[str],
        changed_files: Sequence[str],
        verification_failed: bool,
    ) -> dict[str, object]:
        try:
            contract.validate_before_tool(
                tool.name,
                arguments,
                inspected_files=inspected_files,
                changed_files=changed_files,
                verification_failed=verification_failed,
                mutates_workspace=spec.mutates_workspace,
                is_mcp=spec.required_permission is ToolPermission.MCP,
            )
        except ContractViolation as violation:
            return _contract_violation_payload(spec, arguments, violation)

        snapshot = self._snapshots.capture() if _requires_workspace_snapshot(spec) else None
        if snapshot is not None and snapshot.special_paths:
            paths = ", ".join(sorted(snapshot.special_paths)[:3])
            return _contract_violation_payload(
                spec,
                arguments,
                ContractViolation(
                    "Tool contract violation: side-effect tools are denied while the protected workspace "
                    f"contains symbolic links or other non-regular paths: {paths}."
                ),
            )
        try:
            result = await tool.ainvoke(arguments, config=config)
        except asyncio.CancelledError:
            if snapshot is not None:
                try:
                    snapshot.restore()
                except Exception as rollback_error:
                    raise asyncio.CancelledError(
                        f"workspace rollback failed: {type(rollback_error).__name__}: {rollback_error}"
                    ) from rollback_error
            raise
        except Exception as error:
            if snapshot is None:
                return _tool_exception_payload(spec, arguments, error)
            changes = snapshot.changes()
            violation: ContractViolation | None = None
            try:
                contract.validate_after_tool(
                    tool.name,
                    arguments,
                    _tool_exception_payload(spec, arguments, error),
                    changed_files=changes.changed_files,
                    created_files=changes.created_files,
                    removed_symbols=changes.removed_symbols,
                    unsafe_paths=changes.unsafe_paths,
                )
            except ContractViolation as caught:
                violation = caught
            try:
                snapshot.restore()
            except Exception as rollback_error:
                return _tool_exception_payload(
                    spec,
                    arguments,
                    error,
                    changes=changes,
                    violation=violation,
                    rollback_error=rollback_error,
                )
            return _tool_exception_payload(
                spec, arguments, error, changes=changes, violation=violation
            )
        payload = _tool_result_payload(result, spec, arguments)
        if snapshot is None:
            return payload

        changes = snapshot.changes()
        try:
            contract.validate_after_tool(
                tool.name,
                arguments,
                payload,
                changed_files=changes.changed_files,
                created_files=changes.created_files,
                removed_symbols=changes.removed_symbols,
                unsafe_paths=changes.unsafe_paths,
            )
        except ContractViolation as violation:
            try:
                snapshot.restore()
            except Exception as rollback_error:
                return _rollback_failure_payload(spec, arguments, violation, rollback_error)
            return _contract_violation_payload(spec, arguments, violation, changes=changes)

        metadata = _metadata_with_changes(payload.get("metadata"), changes)
        payload["metadata"] = metadata
        return payload


def _requires_workspace_snapshot(spec: ToolSpec) -> bool:
    return (
        spec.mutates_workspace
        or spec.executes_code
        or spec.required_permission is ToolPermission.MCP
    )


def _tool_result_payload(
    result: object, spec: ToolSpec, arguments: Mapping[str, object]
) -> dict[str, object]:
    if isinstance(result, Mapping):
        return {str(key): value for key, value in result.items()}
    return {
        "name": spec.name,
        "arguments": dict(arguments),
        "content": result,
        "is_error": False,
        "failure_kind": None,
        "retryable": False,
        "permission": spec.required_permission.value,
        "risk": spec.risk.value,
        "metadata": {},
    }


def _contract_violation_payload(
    spec: ToolSpec,
    arguments: Mapping[str, object],
    violation: ContractViolation,
    *,
    changes: WorkspaceChanges | None = None,
) -> dict[str, object]:
    metadata: dict[str, object] = {"blocked_by": "task_contract"}
    if changes is not None:
        metadata["rolled_back"] = changes.metadata()
    return {
        "name": spec.name,
        "arguments": dict(arguments),
        "content": f"TaskContract: {violation}",
        "is_error": True,
        "failure_kind": "task_contract",
        "retryable": False,
        "repair_guidance": "Inspect the repository and satisfy the task contract before retrying.",
        "permission": spec.required_permission.value,
        "risk": spec.risk.value,
        "mcp_server": spec.mcp_server,
        "metadata": metadata,
    }


def _rollback_failure_payload(
    spec: ToolSpec,
    arguments: Mapping[str, object],
    violation: ContractViolation,
    rollback_error: BaseException,
) -> dict[str, object]:
    return {
        "name": spec.name,
        "arguments": dict(arguments),
        "content": (
            f"TaskContract: {violation}; rollback failed: "
            f"{type(rollback_error).__name__}: {rollback_error}"
        ),
        "is_error": True,
        "failure_kind": "workspace_rollback_failed",
        "retryable": False,
        "permission": spec.required_permission.value,
        "risk": spec.risk.value,
        "mcp_server": spec.mcp_server,
        "metadata": {
            "blocked_by": "task_contract",
            "rollback_failed": True,
            "original_failure_kind": "task_contract",
            "rollback_error_type": type(rollback_error).__name__,
        },
    }


def _tool_exception_payload(
    spec: ToolSpec,
    arguments: Mapping[str, object],
    error: Exception,
    *,
    changes: WorkspaceChanges | None = None,
    violation: ContractViolation | None = None,
    rollback_error: Exception | None = None,
) -> dict[str, object]:
    classification = FailureClassifier().classify_exception(error, spec.name)
    content = f"{type(error).__name__}: {error}"
    metadata: dict[str, object] = {"exception_type": type(error).__name__}
    if changes is not None:
        metadata["workspace_changes"] = changes.metadata()
        metadata["rolled_back"] = rollback_error is None
    if violation is not None:
        metadata["task_contract_violation"] = str(violation)
    if rollback_error is not None:
        content += f"; rollback failed: {type(rollback_error).__name__}: {rollback_error}"
        metadata["rollback_failed"] = True
        metadata["original_failure_kind"] = _failure_value(classification.kind)
        metadata["rollback_error_type"] = type(rollback_error).__name__
    return {
        "name": spec.name,
        "arguments": dict(arguments),
        "content": content,
        "is_error": True,
        "failure_kind": (
            "workspace_rollback_failed"
            if rollback_error is not None
            else _failure_value(classification.kind)
        ),
        "retryable": classification.retryable,
        "repair_guidance": classification.repair_guidance,
        "permission": spec.required_permission.value,
        "risk": spec.risk.value,
        "mcp_server": spec.mcp_server,
        "metadata": metadata,
    }


def _metadata_with_changes(value: object, changes: WorkspaceChanges) -> dict[str, object]:
    metadata = dict(value) if isinstance(value, Mapping) else {}
    metadata["workspace_changes"] = changes.metadata()
    return metadata


def _capture_workspace(root: Path) -> tuple[dict[str, _WorkspaceFile], set[str], set[str]]:
    files: dict[str, _WorkspaceFile] = {}
    directories: set[str] = set()
    special_paths: set[str] = set()
    for current, dirnames, filenames in os.walk(root):
        current_path = Path(current)
        next_directories: list[str] = []
        for name in sorted(dirnames):
            if name in _SNAPSHOT_IGNORED_DIRECTORIES:
                continue
            candidate = current_path / name
            try:
                status = candidate.stat(follow_symlinks=False)
            except OSError:
                continue
            relative = candidate.relative_to(root).as_posix()
            if stat.S_ISDIR(status.st_mode):
                next_directories.append(name)
            else:
                special_paths.add(relative)
        dirnames[:] = next_directories
        relative_directory = current_path.relative_to(root).as_posix()
        if relative_directory != ".":
            directories.add(relative_directory)
        for filename in sorted(filenames):
            path = current_path / filename
            try:
                status = path.stat(follow_symlinks=False)
            except OSError:
                continue
            if not stat.S_ISREG(status.st_mode):
                special_paths.add(path.relative_to(root).as_posix())
                continue
            try:
                content = path.read_bytes()
            except OSError:
                continue
            files[path.relative_to(root).as_posix()] = _WorkspaceFile(
                content, stat.S_IMODE(status.st_mode)
            )
    return files, directories, special_paths


def _python_symbols(content: bytes) -> set[str]:
    try:
        tree = ast.parse(content.decode("utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return set()
    symbols: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.add(node.name)
        elif isinstance(node, ast.ClassDef):
            symbols.add(node.name)
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    symbols.add(f"{node.name}.{item.name}")
    return symbols
