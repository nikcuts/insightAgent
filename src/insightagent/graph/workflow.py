"""Compiled LangGraph workflow for one repository repair turn."""

from __future__ import annotations

from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from insightagent.graph.nodes import (
    GraphServices,
    action_required,
    call_model,
    execute_tools,
    fail,
    inject_repository_snapshot,
    prepare_task,
    repair,
    route_after_repair,
    route_after_model,
    route_after_tools,
    summarize,
    trim_context,
)
from insightagent.graph.state import AgentState


def build_graph(services: GraphServices, checkpointer: Any | None = None):
    """Build a graph whose tool calls all pass through the contract invoker."""
    async def prepare_node(state: AgentState, config: RunnableConfig) -> AgentState:
        return _record_phase_transition(
            services, "prepare_task", state, await prepare_task(state, config, services)
        )

    async def snapshot_node(state: AgentState, config: RunnableConfig) -> AgentState:
        return _record_phase_transition(
            services,
            "inject_repository_snapshot",
            state,
            await inject_repository_snapshot(state, config, services),
        )

    async def model_node(state: AgentState, config: RunnableConfig) -> AgentState:
        return _record_phase_transition(
            services, "call_model", state, await call_model(state, config, services)
        )

    async def trim_node(state: AgentState, config: RunnableConfig) -> AgentState:
        return _record_phase_transition(
            services, "trim_context", state, await trim_context(state, config, services)
        )

    async def tools_node(state: AgentState, config: RunnableConfig) -> AgentState:
        return _record_phase_transition(
            services, "execute_tools", state, await execute_tools(state, config, services)
        )

    async def repair_node(state: AgentState, config: RunnableConfig) -> AgentState:
        return _record_phase_transition(
            services, "repair", state, await repair(state, config, services)
        )

    async def summarize_node(state: AgentState, config: RunnableConfig) -> AgentState:
        return _record_phase_transition(
            services, "summarize", state, await summarize(state, config, services)
        )

    async def fail_node(state: AgentState, config: RunnableConfig) -> AgentState:
        return _record_phase_transition(services, "fail", state, await fail(state, config, services))

    def action_node(state: AgentState) -> AgentState:
        return _record_phase_transition(services, "action_required", state, action_required(state))

    graph = StateGraph(AgentState)
    graph.add_node("prepare_task", prepare_node)
    graph.add_node("inject_repository_snapshot", snapshot_node)
    graph.add_node("trim_context", trim_node)
    graph.add_node("call_model", model_node)
    graph.add_node("execute_tools", tools_node)
    graph.add_node("repair", repair_node)
    graph.add_node("summarize", summarize_node)
    graph.add_node("action_required", action_node)
    graph.add_node("fail", fail_node)
    # Checkpoint-only node used by the runner to persist run manifests without
    # re-entering the model/tool loop.
    graph.add_node("record_manifest", lambda _state: {})

    graph.add_edge(START, "prepare_task")
    graph.add_edge("prepare_task", "inject_repository_snapshot")
    graph.add_edge("inject_repository_snapshot", "trim_context")
    graph.add_edge("trim_context", "call_model")
    graph.add_conditional_edges(
        "call_model",
        lambda state: route_after_model(state, services),
        {
            "execute_tools": "execute_tools",
            "summarize": "summarize",
            "action_required": "action_required",
            "fail": "fail",
        },
    )
    graph.add_conditional_edges(
        "execute_tools",
        lambda state: route_after_tools(state, services),
        {
            "call_model": "trim_context",
            "repair": "repair",
            "summarize": "summarize",
            "fail": "fail",
        },
    )
    graph.add_conditional_edges(
        "repair", route_after_repair, {"call_model": "trim_context", "fail": "fail"}
    )
    graph.add_edge("action_required", "fail")
    graph.add_edge("summarize", END)
    graph.add_edge("fail", END)
    return graph.compile(checkpointer=checkpointer)


def _record_phase_transition(
    services: GraphServices,
    node_name: str,
    previous: AgentState,
    update: AgentState,
) -> AgentState:
    observability = services.observability
    previous_phase = previous.get("phase")
    next_phase = update.get("phase", previous_phase)
    if observability is not None and previous_phase != next_phase:
        observability.record_decision(
            "phase_transition",
            {
                "from_phase": previous_phase,
                "phase": next_phase,
                "node": node_name,
            },
        )
    return update


__all__ = ["GraphServices", "build_graph"]
