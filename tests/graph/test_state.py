"""图状态必须仅包含可检查点化的数据。"""

from __future__ import annotations

import asyncio
import json
from typing import cast

from langchain_core.messages import AIMessage, HumanMessage, message_to_dict
import pytest

from tests.graph.fakes import ScriptedRunnable


def test_message_reducer_appends_langchain_messages() -> None:
    from insightagent.graph.state import merge_messages

    merged = merge_messages([HumanMessage(content="a")], [AIMessage(content="b")])

    assert [message.content for message in merged] == ["a", "b"]


def test_message_reducer_replaces_equal_ids_and_preserves_empty_update() -> None:
    from insightagent.graph.state import merge_messages

    left = [HumanMessage(content="旧内容", id="message-1")]
    right = [AIMessage(content="新内容", id="message-1")]

    assert merge_messages(left, right) == right
    assert merge_messages(left, []) == left


def test_new_turn_update_resets_turn_scoped_fields() -> None:
    from insightagent.graph.state import new_turn_update

    update = new_turn_update("修复 calc.py")

    assert update["messages"] == [HumanMessage(content="修复 calc.py")]
    assert update["task"] == "修复 calc.py"
    assert update["phase"] == "plan"
    assert update["iteration"] == 0
    assert update["verification_command"] is None
    assert update["last_tool_error"] is None
    assert update["final_answer"] is None
    assert update["changed_files"] == []
    assert update["inspected_files"] == []
    assert update["verification_attempts"] == []
    assert update["repair_attempts"] == 0
    assert update["phase_history"] == ["plan"]
    assert update["tool_events"] == []


def test_new_turn_update_excludes_runtime_deadline() -> None:
    from insightagent.graph.state import new_turn_update

    assert "deadline_monotonic" not in new_turn_update("执行任务")


def test_new_turn_update_is_serializable_with_langchain_message_codec() -> None:
    from insightagent.graph.state import new_turn_update

    update = new_turn_update("执行任务")
    serialized = {
        key: [message_to_dict(message) for message in value]
        if key == "messages"
        else value
        for key, value in update.items()
    }

    assert json.loads(json.dumps(serialized, ensure_ascii=False))["task"] == "执行任务"


def test_event_fields_use_the_recursive_json_value_alias() -> None:
    from insightagent.graph.state import AgentState, JSONValue

    verification_attempts = AgentState.__annotations__["verification_attempts"]
    tool_events = AgentState.__annotations__["tool_events"]

    assert verification_attempts.__forward_arg__ == "list[dict[str, JSONValue]]"
    assert tool_events.__forward_arg__ == "list[dict[str, JSONValue]]"
    assert JSONValue is not object


def test_scripted_runnable_records_direct_configs_and_supports_bound_paths() -> None:
    runnable = ScriptedRunnable(["sync", "async", "bound sync", "bound async"])
    sync_config = {"tags": ["sync"]}
    async_config = {"tags": ["async"]}
    bound_sync_config = {"tags": ["bound-sync"]}
    bound_async_config = {"tags": ["bound-async"]}

    assert runnable.invoke("input", sync_config).content == "sync"
    assert asyncio.run(runnable.ainvoke("input", async_config)).content == "async"

    bound = runnable.bind(mode="test")
    assert bound.invoke("input", bound_sync_config).content == "bound sync"
    assert (
        asyncio.run(bound.ainvoke("input", bound_async_config)).content == "bound async"
    )

    assert runnable.invoke_configs[0] == sync_config
    assert runnable.ainvoke_configs[0] == async_config
    assert runnable.invoke_configs[1]["tags"] == bound_sync_config["tags"]
    assert runnable.ainvoke_configs[1]["tags"] == bound_async_config["tags"]
    assert runnable.invoke_kwargs == [{}, {"mode": "test"}]
    assert runnable.ainvoke_kwargs == [{}, {"mode": "test"}]


def test_scripted_runnable_fails_clearly_when_its_script_is_exhausted() -> None:
    runnable = ScriptedRunnable([])

    with pytest.raises(AssertionError, match="响应脚本已耗尽"):
        runnable.invoke("input")


def test_async_sqlite_saver_persists_json_events_and_rejects_objects() -> None:
    from insightagent.graph.state import AgentState, JSONValue
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from langgraph.graph import END, START, StateGraph

    def retain_tool_events(state: AgentState) -> AgentState:
        return {"tool_events": state["tool_events"]}

    async def run_probe() -> None:
        async with AsyncSqliteSaver.from_conn_string(":memory:") as checkpointer:
            workflow = StateGraph(AgentState)
            workflow.add_node("retain_tool_events", retain_tool_events)
            workflow.add_edge(START, "retain_tool_events")
            workflow.add_edge("retain_tool_events", END)
            graph = workflow.compile(checkpointer=checkpointer)

            event = {"kind": "tool", "details": {"passed": True, "count": 1}}
            config = {"configurable": {"thread_id": "json-event"}}
            updates = [
                update
                async for update in graph.astream(
                    {"tool_events": [event]}, config, stream_mode="updates"
                )
            ]
            snapshot = await graph.aget_state(config)

            assert updates
            assert snapshot.values["tool_events"] == [event]

            invalid_event = cast(JSONValue, object())
            with pytest.raises(TypeError, match=r"(?i)(msgpack|serializ)"):
                await graph.ainvoke(
                    {"tool_events": [{"kind": "invalid", "payload": invalid_event}]},
                    {"configurable": {"thread_id": "invalid-event"}},
                )

    asyncio.run(run_probe())
