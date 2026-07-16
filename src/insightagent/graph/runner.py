"""Resource-owning runtime for persisted LangGraph coding turns."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, cast

from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool

from insightagent.cli.tool_profiles import filter_tools, select_mcp_config
from insightagent.config import RuntimeConfig, load_dotenv_files
from insightagent.graph.checkpoints import CheckpointStore, open_checkpointer
from insightagent.graph.models import build_chat_model
from insightagent.graph.nodes import GraphServices
from insightagent.graph.observability import (
    GraphConsoleRenderer,
    GraphDebugRecorder,
    GraphObservability,
    build_observability,
)
from insightagent.graph.project_memory import ProjectMemory, load_project_memory
from insightagent.graph.sessions import GraphSessionService, build_checkpoint_reader
from insightagent.graph.state import AgentState, trim_message_prefix
from insightagent.graph.tools import ContractAwareToolInvoker, ToolRuntime, build_builtin_tools
from insightagent.graph.workflow import build_graph
from insightagent.mcp.config import MCPConfig, load_mcp_config
from insightagent.mcp.errors import MCPStartupError
from insightagent.mcp.manager import MCPManager
from insightagent.runtime.tool_context import ToolContext
from insightagent.runtime.types import ToolSpec


@dataclass(frozen=True)
class RunOutcome:
    """Stable result returned by the graph runner and one-shot CLI."""

    final_answer: str
    state: AgentState
    thread_id: str
    checkpoint_id: str | None
    trace_id: str | None


class GraphRunner:
    """Own every runtime dependency needed for one or more graph turns."""

    def __init__(
        self,
        *,
        config: RuntimeConfig,
        workspace: Path,
        session_dir: Path,
        tool_profile: str,
        allowed_tools: set[str] | None,
        enabled_mcp_servers: set[str],
        trace_jsonl: str | None,
        no_trace: bool,
        mcp_config: MCPConfig | None = None,
    ) -> None:
        self.config = config
        self.workspace = workspace.expanduser().resolve()
        self.session_dir = session_dir.expanduser().resolve()
        self.tool_profile = tool_profile
        self.allowed_tools = allowed_tools
        self.enabled_mcp_servers = set(enabled_mcp_servers)
        self.trace_jsonl = trace_jsonl
        self.no_trace = no_trace
        self._configured_mcp = mcp_config
        self._store: CheckpointStore | None = None
        self._mcp_manager: MCPManager | None = None
        self._model: Runnable | None = None
        self._tools: dict[str, BaseTool] = {}
        self._tool_specs: dict[str, ToolSpec] = {}
        self._builtin_tools: list[BaseTool] = []
        self._builtin_specs: dict[str, ToolSpec] = {}
        self._tool_context: ToolContext | None = None
        self._debug_recorder: GraphDebugRecorder | None = None
        self._console_renderer = (
            None
            if no_trace
            else GraphConsoleRenderer(max_chars=self.config.trace_max_chars)
        )
        self._observability: GraphObservability | None = None
        self._lifecycle_observability: GraphObservability | None = None
        self._project_memory = ProjectMemory()
        self._last_trace_ids: dict[str, str] = {}
        self._closing = False

    async def __aenter__(self) -> "GraphRunner":
        self.workspace.mkdir(parents=True, exist_ok=True)
        self._store = await open_checkpointer(self.session_dir)
        try:
            self._tool_context = ToolContext(
                workspace=self.workspace,
                permission_mode=self.config.permission_mode,
            )
            self._project_memory = load_project_memory(self.workspace)
            runtime = ToolRuntime(self._tool_context)
            builtin_tools = filter_tools(
                build_builtin_tools(self._tool_context, runtime),
                self.tool_profile,
                self.allowed_tools,
            )
            builtin_specs = runtime.specs()
            self._builtin_tools = builtin_tools
            self._builtin_specs = {
                tool.name: builtin_specs[tool.name] for tool in builtin_tools
            }
            if self.trace_jsonl:
                self._debug_recorder = GraphDebugRecorder(
                    self.trace_jsonl, max_chars=self.config.trace_max_chars
                )
            self._lifecycle_observability = build_observability(
                workspace=str(self.workspace),
                thread_id=f"runner-{uuid.uuid4().hex}",
                provider=self.config.provider,
                model=self.config.model or "",
                tool_profile=self.tool_profile,
                trace_max_chars=self.config.trace_max_chars,
            )
            mcp_config = select_mcp_config(
                self._configured_mcp or load_mcp_config(self.workspace), self.enabled_mcp_servers
            )
            self._mcp_manager = MCPManager(mcp_config, self._tool_context)
            with self._lifecycle_observability.turn("insightagent-runner-startup"):
                await self._mcp_manager.start_enabled(trace=self._record_mcp_event)
            failed_servers = getattr(self._mcp_manager, "failed", {})
            if isinstance(failed_servers, Mapping) and failed_servers:
                raise MCPStartupError(failed_servers)
            await self._rebuild_tool_surface()
            return self
        except BaseException:
            await self._close_resources()
            raise

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        await self._close_resources()

    async def run_turn(
        self,
        task: str,
        *,
        session_id: str | None,
        max_wall_seconds: float | None = None,
    ) -> RunOutcome:
        store, model, context = self._require_started()
        thread_id = session_id or uuid.uuid4().hex
        observability = build_observability(
            workspace=str(self.workspace),
            thread_id=thread_id,
            provider=self.config.provider,
            model=self.config.model or "",
            task=task,
            tool_profile=self.tool_profile,
            event_sink=self._record_graph_event,
            trace_max_chars=self.config.trace_max_chars,
        )
        self._observability = observability
        services = GraphServices(
            model=model,
            tools=self._tools,
            tool_specs=self._tool_specs,
            tool_context=context,
            tool_invoker=ContractAwareToolInvoker(context),
            max_iterations=self.config.max_tool_iterations,
            max_tool_output_chars=self.config.max_tool_output_chars,
            compact_tool_output_chars=self.config.compact_tool_output_chars,
            project_memory=self._project_memory.render(),
            language=self.config.response_language,
            observability=observability,
        )
        graph = build_graph(services, checkpointer=store.checkpointer)
        sessions = GraphSessionService(graph, store)
        deadline = _deadline(max_wall_seconds if max_wall_seconds is not None else self.config.max_wall_seconds)
        try:
            with observability.turn("insightagent-turn"):
                turn = await sessions.start_turn(
                    thread_id,
                    self.workspace,
                    task,
                    deadline_monotonic=deadline,
                    run_config=observability.runnable_config(),
                )
        finally:
            observability.flush()
        state = turn.state
        trace_id = observability.trace_id
        if trace_id is None:
            self._last_trace_ids.pop(thread_id, None)
        else:
            self._last_trace_ids[thread_id] = trace_id
        if self._debug_recorder is not None:
            self._debug_recorder.record_turn(thread_id, state, trace_id=trace_id)
        if self._console_renderer is not None:
            self._console_renderer.render(
                {
                    "type": "graph_turn",
                    "thread_id": thread_id,
                    "phase": state.get("phase"),
                    "trace_id": trace_id,
                }
            )
        return RunOutcome(
            final_answer=str(state.get("final_answer") or ""),
            state=state,
            thread_id=thread_id,
            checkpoint_id=turn.checkpoint_id,
            trace_id=trace_id,
        )

    async def list_sessions(self) -> list[str]:
        store, _model, _context = self._require_started()
        return await GraphSessionService(None, store).list_threads()

    @property
    def permission_mode(self) -> str:
        return self.config.permission_mode

    @property
    def project_memory_filenames(self) -> tuple[str, ...]:
        """Names of project guidance files loaded for newly executed turns."""
        return self._project_memory.filenames

    def last_trace_id(self, thread_id: str) -> str | None:
        return self._last_trace_ids.get(thread_id)

    async def latest_state(self, thread_id: str) -> Any:
        store, _model, _context = self._require_started()
        reader = build_checkpoint_reader(store.checkpointer)
        return await GraphSessionService(reader, store).latest_state(thread_id)

    async def get_checkpoint(self, thread_id: str, checkpoint_id: str) -> Any:
        store, _model, _context = self._require_started()
        reader = build_checkpoint_reader(store.checkpointer)
        return await GraphSessionService(reader, store).checkpoint_state(thread_id, checkpoint_id)

    async def export_transcript(self, thread_id: str, destination: Path) -> Path:
        store, _model, _context = self._require_started()
        reader = build_checkpoint_reader(store.checkpointer)
        return await GraphSessionService(reader, store).export_markdown(thread_id, destination)

    async def compact_thread(self, thread_id: str) -> dict[str, int]:
        """Trim historical messages and tool events through a checkpoint update."""
        store, _model, _context = self._require_started()
        reader = build_checkpoint_reader(store.checkpointer)
        sessions = GraphSessionService(reader, store)
        snapshot = await sessions.latest_state(thread_id)
        values = getattr(snapshot, "values", {})
        state = values if isinstance(values, Mapping) else {}
        messages = state.get("messages", [])
        removals, _retained = trim_message_prefix(messages, 20) if isinstance(messages, list) else ([], [])
        events = state.get("tool_events", [])
        event_list = list(events) if isinstance(events, list) else []
        omitted_events = max(0, len(event_list) - 20)
        update: dict[str, object] = {}
        if removals:
            update["messages"] = removals
        if omitted_events:
            update["tool_events"] = [
                {"type": "compacted_tool_events", "omitted": omitted_events},
                *event_list[-20:],
            ]
        if update:
            await sessions.update_state(thread_id, update, as_node="checkpoint_reader")
        return {
            "removed_messages": len(removals),
            "summarized_tool_events": omitted_events,
        }

    async def mcp_status(self) -> dict[str, dict[str, object]]:
        return cast(dict[str, dict[str, object]], await self._require_mcp_manager().status())

    async def mcp_tool_names(self) -> list[str]:
        return [tool.name for tool in await self._require_mcp_manager().get_tools()]

    async def restart_mcp(self, server: str) -> bool:
        restarted = await self._require_mcp_manager().restart_server(
            server, trace=self._record_mcp_event
        )
        if restarted:
            await self._rebuild_tool_surface()
        return restarted

    async def refresh_mcp(self, server: str) -> bool:
        refreshed = await self._require_mcp_manager().refresh_server(
            server, trace=self._record_mcp_event
        )
        if refreshed:
            await self._rebuild_tool_surface()
        return refreshed

    async def _rebuild_tool_surface(self) -> None:
        """Rebind the model to the current MCP generation for subsequent turns."""
        manager = self._require_mcp_manager()
        mcp_tools = await manager.get_tools()
        mcp_specs = await manager.get_tool_specs()
        all_tools = [*self._builtin_tools, *mcp_tools]
        names = [tool.name for tool in all_tools]
        if len(names) != len(set(names)):
            raise ValueError("duplicate tool names after MCP tool loading")
        model = build_chat_model(self.config, all_tools)
        self._tools = {tool.name: tool for tool in all_tools}
        self._tool_specs = {**self._builtin_specs, **mcp_specs}
        self._model = model

    async def _close_resources(self) -> None:
        if self._closing:
            return
        self._closing = True
        manager, store, recorder, observability, lifecycle_observability = (
            self._mcp_manager,
            self._store,
            self._debug_recorder,
            self._observability,
            self._lifecycle_observability,
        )

        first_error: BaseException | None = None

        async def close_async(closer: Awaitable[object]) -> None:
            nonlocal first_error
            try:
                await closer
            except BaseException as error:
                if first_error is None:
                    first_error = error

        def close_sync(closer: Callable[[], None]) -> None:
            nonlocal first_error
            try:
                closer()
            except BaseException as error:
                if first_error is None:
                    first_error = error

        try:
            if manager is not None:
                if lifecycle_observability is not None:
                    with lifecycle_observability.turn("insightagent-runner-shutdown"):
                        await manager.stop_all(trace=self._record_mcp_event)
                else:
                    await manager.stop_all(trace=self._record_mcp_event)
        except BaseException as error:
            first_error = error
        if recorder is not None:
            close_sync(recorder.close)
        if observability is not None:
            close_sync(observability.close)
        if lifecycle_observability is not None:
            close_sync(lifecycle_observability.close)
        if store is not None:
            await close_async(store.close())
        self._mcp_manager = None
        self._store = None
        self._debug_recorder = None
        self._console_renderer = None
        self._observability = None
        self._lifecycle_observability = None
        self._closing = False
        if first_error is not None:
            raise first_error

    def _require_started(self) -> tuple[CheckpointStore, Runnable, ToolContext]:
        if self._store is None or self._model is None or self._tool_context is None:
            raise RuntimeError("GraphRunner is not started")
        return self._store, self._model, self._tool_context

    def _require_mcp_manager(self) -> MCPManager:
        if self._mcp_manager is None:
            raise RuntimeError("GraphRunner is not started")
        return self._mcp_manager

    def _record_mcp_event(self, event: dict[str, Any]) -> None:
        self._record_graph_event(event)
        if self._lifecycle_observability is not None:
            self._lifecycle_observability.record_decision("mcp_lifecycle", event)

    def _record_graph_event(self, event: Mapping[str, object]) -> None:
        if self._debug_recorder is not None:
            self._debug_recorder.record_event(event)
        if self._console_renderer is not None:
            self._console_renderer.render(event)


def run_task(
    *,
    task: str,
    workspace: Path,
    config: RuntimeConfig,
    session_id: str | None,
    checkpoint_id: str | None,
    tool_profile: str,
    allowed_tools: set[str] | None,
    enabled_mcp_servers: set[str],
    trace_jsonl: str | None,
    no_trace: bool,
    mcp_config: MCPConfig | None = None,
) -> RunOutcome:
    """Synchronously execute one graph turn for the command-line entry point."""
    workspace = workspace.expanduser().resolve()
    load_dotenv_files(workspace)
    if checkpoint_id is not None:
        if session_id is None:
            raise ValueError("checkpoint_id requires session_id")
        return asyncio.run(
            _inspect_checkpoint(
                _resolve_session_dir(config, workspace), session_id, checkpoint_id
            )
        )

    async def run() -> RunOutcome:
        async with GraphRunner(
            config=config,
            workspace=workspace,
            session_dir=_resolve_session_dir(config, workspace),
            tool_profile=tool_profile,
            allowed_tools=allowed_tools,
            enabled_mcp_servers=enabled_mcp_servers,
            trace_jsonl=trace_jsonl,
            no_trace=no_trace,
            mcp_config=mcp_config,
        ) as runner:
            return await runner.run_turn(task, session_id=session_id)

    return asyncio.run(run())


async def _inspect_checkpoint(
    session_dir: Path,
    thread_id: str,
    checkpoint_id: str,
) -> RunOutcome:
    """Read one historical checkpoint without constructing a graph runtime."""
    store = await open_checkpointer(session_dir)
    try:
        graph = build_checkpoint_reader(store.checkpointer)
        snapshot = await GraphSessionService(graph, store).checkpoint_state(thread_id, checkpoint_id)
        values = getattr(snapshot, "values", None)
        if not isinstance(values, Mapping) or not values:
            raise ValueError(f"checkpoint not found: {checkpoint_id}")
        state = cast(AgentState, dict(values))
        return RunOutcome(
            final_answer=str(state.get("final_answer") or ""),
            state=state,
            thread_id=thread_id,
            checkpoint_id=checkpoint_id,
            trace_id=None,
        )
    finally:
        await store.close()


def _resolve_session_dir(config: RuntimeConfig, workspace: Path) -> Path:
    session_dir = Path(config.session_dir).expanduser()
    return session_dir if session_dir.is_absolute() else workspace / session_dir


def _deadline(max_wall_seconds: float) -> float | None:
    return time.monotonic() + max_wall_seconds if max_wall_seconds > 0 else None


__all__ = ["GraphRunner", "RunOutcome", "run_task"]
