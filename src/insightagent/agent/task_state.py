"""Task lifecycle state machine for coding-agent turns."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class TaskPhase(Enum):
    PLAN = "plan"
    IMPLEMENT = "implement"
    VERIFY = "verify"
    REPAIR = "repair"
    SUMMARIZE = "summarize"
    DONE = "done"
    FAILED = "failed"


@dataclass
class TaskState:
    phase: TaskPhase = TaskPhase.PLAN
    verification_attempts: int = 0
    repair_attempts: int = 0
    max_repairs: int = 3
    last_error: str | None = None


IMPLEMENTATION_TOOLS = {"write_file", "edit_file", "apply_edits", "todo_write"}
VERIFICATION_TOOLS = {"execute_command", "run_verification", "lsp_diagnostics"}


def phase_instruction(state: TaskState) -> str:
    if state.phase == TaskPhase.PLAN:
        guidance = "Produce a short plan, then use tools for real work."
    elif state.phase == TaskPhase.IMPLEMENT:
        guidance = (
            "Inspect, create, or edit files. When the code is in place, call `run_verification` "
            "to test/compile the project."
        )
    elif state.phase == TaskPhase.VERIFY:
        guidance = (
            "Call `run_verification` (it auto-detects pytest / npm test / python compile) and "
            "report the exact exit_code and output."
        )
    elif state.phase == TaskPhase.REPAIR:
        guidance = (
            "Use the latest error to edit the code, then call `run_verification` again until it "
            "passes."
        )
    elif state.phase == TaskPhase.SUMMARIZE:
        guidance = "Give the final summary with changed files, verification, and remaining risks."
    elif state.phase == TaskPhase.FAILED:
        guidance = "Stop and explain why the task could not be repaired within the limit."
    else:
        guidance = "The task is complete."
    return f"Current task phase: {state.phase.value}. {guidance}"


def transition_after_tool(
    state: TaskState,
    tool_name: str,
    tool_content: str,
    is_error: bool,
) -> tuple[TaskPhase, TaskPhase]:
    old_phase = state.phase
    if state.phase in {TaskPhase.DONE, TaskPhase.FAILED}:
        return old_phase, state.phase
    if is_error:
        _enter_repair_or_failed(state, tool_content)
        return old_phase, state.phase
    if tool_name in IMPLEMENTATION_TOOLS:
        state.phase = TaskPhase.VERIFY if old_phase == TaskPhase.REPAIR else TaskPhase.IMPLEMENT
    elif tool_name in VERIFICATION_TOOLS:
        state.verification_attempts += 1
        if _verification_succeeded(tool_name, tool_content):
            state.phase = TaskPhase.SUMMARIZE
            state.last_error = None
        else:
            _enter_repair_or_failed(state, tool_content)
    return old_phase, state.phase


def mark_final_answer(state: TaskState) -> tuple[TaskPhase, TaskPhase]:
    old_phase = state.phase
    state.phase = TaskPhase.FAILED if state.phase == TaskPhase.FAILED else TaskPhase.DONE
    return old_phase, state.phase


def _enter_repair_or_failed(state: TaskState, error: str) -> None:
    state.last_error = error
    state.repair_attempts += 1
    state.phase = TaskPhase.FAILED if state.repair_attempts >= state.max_repairs else TaskPhase.REPAIR


def _verification_succeeded(tool_name: str, tool_content: str) -> bool:
    if tool_name in {"execute_command", "run_verification"}:
        return tool_content.startswith("exit_code: 0\n")
    if tool_name == "lsp_diagnostics":
        return tool_content.strip() == "no diagnostics"
    return False
