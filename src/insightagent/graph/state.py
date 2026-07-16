"""仅由检查点可序列化数据组成的 LangGraph 状态。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Literal, TypeAlias, TypedDict, cast

from langchain_core.messages import (
    AnyMessage,
    BaseMessage,
    HumanMessage,
    MessageLikeRepresentation,
    RemoveMessage,
    ToolMessage,
)
from langgraph.graph.message import add_messages

Phase = Literal[
    "plan",
    "inspect",
    "implement",
    "verify",
    "repair",
    "summarize",
    "done",
    "failed",
]

JSONValue: TypeAlias = (
    str | int | float | bool | None | list["JSONValue"] | dict[str, "JSONValue"]
)


def merge_messages(left: list[AnyMessage], right: list[AnyMessage]) -> list[AnyMessage]:
    """按 LangGraph 的消息 ID 规则合并消息增量。"""
    return cast(
        list[AnyMessage],
        add_messages(
            cast(list[MessageLikeRepresentation], cast(list[BaseMessage], left)),
            cast(list[MessageLikeRepresentation], cast(list[BaseMessage], right)),
        ),
    )


def trim_message_prefix(
    messages: Sequence[BaseMessage], max_messages: int
) -> tuple[list[RemoveMessage], list[BaseMessage]]:
    """裁剪历史前缀，但始终保留最新的完整工具调用消息组。"""
    if len(messages) <= max_messages:
        return [], list(messages)
    cutoff = len(messages) - max_messages
    while cutoff > 0 and isinstance(messages[cutoff], ToolMessage):
        cutoff -= 1
    removable = messages[:cutoff]
    removals = [
        RemoveMessage(id=message_id)
        for message in removable
        if isinstance((message_id := message.id), str) and message_id
    ]
    return removals, list(messages[cutoff:])


class AgentState(TypedDict, total=False):
    """图节点之间传递并由检查点持久化的纯数据。"""

    messages: Annotated[list[AnyMessage], merge_messages]
    workspace: str
    task: str
    phase: Phase
    iteration: int
    max_iterations: int
    verification_command: str | None
    last_tool_error: str | None
    changed_files: list[str]
    workspace_revision: int
    verified_workspace_revision: int | None
    inspected_files: list[str]
    verification_attempts: list[dict[str, JSONValue]]
    repair_attempts: int
    repair_action_completed: bool
    repair_inspected_since_failure: bool
    phase_history: list[str]
    tool_events: list[dict[str, JSONValue]]
    usage: dict[str, int]
    final_answer: str | None


def new_turn_update(task: str) -> AgentState:
    """创建一轮任务的可检查点化状态增量。"""
    from insightagent.graph.observability import sanitize_for_model_trace_and_persistence

    sanitized_task = str(sanitize_for_model_trace_and_persistence(task))
    return {
        "messages": [HumanMessage(content=sanitized_task)],
        "task": sanitized_task,
        "phase": "plan",
        "iteration": 0,
        "verification_command": None,
        "last_tool_error": None,
        "changed_files": [],
        "workspace_revision": 0,
        "verified_workspace_revision": None,
        "inspected_files": [],
        "verification_attempts": [],
        "repair_attempts": 0,
        "repair_action_completed": False,
        "repair_inspected_since_failure": False,
        "phase_history": ["plan"],
        "tool_events": [],
        "final_answer": None,
    }
