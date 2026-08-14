"""Resource-owning runtime for persisted LangGraph coding turns."""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import os
import subprocess
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
    sanitize_for_model_trace_and_persistence,
)
from insightagent.graph.project_memory import ProjectMemory, load_project_memory
from insightagent.graph.sessions import GraphSessionService, build_checkpoint_reader
from insightagent.graph.state import AgentState, JSONValue, trim_message_prefix
from insightagent.graph.tools import ContractAwareToolInvoker, ToolRuntime, build_builtin_tools
from insightagent.graph.workflow import build_graph
from insightagent.mcp.config import MCPConfig, load_mcp_config
from insightagent.mcp.errors import MCPStartupError
from insightagent.mcp.manager import MCPManager
from insightagent.runtime.tool_context import ToolContext
from insightagent.runtime.types import ToolSpec


_POLICY_VERSION = "runtime-hardening-v1"


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
                approval_mode=self.config.approval_mode,
                execution_mode=self.config.execution_mode,
                sandbox_image=self.config.sandbox_image,
                sandbox_memory_mb=self.config.sandbox_memory_mb,
                sandbox_cpus=self.config.sandbox_cpus,
                sandbox_pids_limit=self.config.sandbox_pids_limit,
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
                model=_effective_model(self.config.model),
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
        run_id = uuid.uuid4().hex
        started_monotonic = time.monotonic()
        observability = build_observability(
            workspace=str(self.workspace),
            thread_id=thread_id,
            provider=self.config.provider,
            model=_effective_model(self.config.model),
            task=task,
            run_id=run_id,
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
        state = cast(AgentState, dict(turn.state))
        trace_id = observability.trace_id
        manifest, checkpoint_id = await self._persist_run_manifest(
            sessions,
            thread_id,
            state,
            run_id=run_id,
            started_monotonic=started_monotonic,
            trace_id=trace_id,
        )
        state["run_manifest"] = manifest
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
            checkpoint_id=checkpoint_id or turn.checkpoint_id,
            trace_id=trace_id,
        )

    async def resume_turn(
        self,
        thread_id: str,
        resume_value: object,
        *,
        max_wall_seconds: float | None = None,
    ) -> RunOutcome:
        """Resume a persisted approval interrupt for ``thread_id``.

        LangGraph resumes from the interrupt checkpoint, so the original tool
        batch is not replayed before the approval decision is applied.
        """
        if self.config.approval_mode != "interrupt":
            raise ValueError("resume_turn requires approval_mode=interrupt")
        store, model, context = self._require_started()
        reader = build_checkpoint_reader(store.checkpointer)
        sessions = GraphSessionService(reader, store)
        snapshot = await sessions.latest_state(thread_id)
        values = getattr(snapshot, "values", {})
        persisted_state = values if isinstance(values, Mapping) else {}
        task = str(persisted_state.get("task") or "resume approval")
        prior_manifest = persisted_state.get("run_manifest") or self._load_manifest(thread_id)
        prior_run_id = (
            str(prior_manifest.get("run_id"))
            if isinstance(prior_manifest, Mapping) and prior_manifest.get("run_id")
            else uuid.uuid4().hex
        )
        started_monotonic = time.monotonic()
        observability = build_observability(
            workspace=str(self.workspace),
            thread_id=thread_id,
            provider=self.config.provider,
            model=_effective_model(self.config.model),
            task=task,
            run_id=prior_run_id,
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
        deadline = _deadline(
            max_wall_seconds
            if max_wall_seconds is not None
            else self.config.max_wall_seconds
        )
        try:
            with observability.turn("insightagent-approval-resume"):
                turn = await sessions.resume_turn(
                    thread_id,
                    self.workspace,
                    resume_value,
                    deadline_monotonic=deadline,
                    run_config=observability.runnable_config(),
                )
        finally:
            observability.flush()
        state = cast(AgentState, dict(turn.state))
        trace_id = observability.trace_id
        manifest, checkpoint_id = await self._persist_run_manifest(
            sessions,
            thread_id,
            state,
            run_id=prior_run_id,
            started_monotonic=started_monotonic,
            trace_id=trace_id,
            prior_manifest=prior_manifest,
            resume_value=resume_value,
        )
        state["run_manifest"] = manifest
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
                    "resumed": True,
                }
            )
        return RunOutcome(
            final_answer=str(state.get("final_answer") or ""),
            state=state,
            thread_id=thread_id,
            checkpoint_id=checkpoint_id or turn.checkpoint_id,
            trace_id=trace_id,
        )

    async def _persist_run_manifest(
        self,
        sessions: GraphSessionService,
        thread_id: str,
        state: AgentState,
        *,
        run_id: str,
        started_monotonic: float,
        trace_id: str | None,
        prior_manifest: object | None = None,
        resume_value: object | None = None,
    ) -> tuple[dict[str, JSONValue], str | None]:
        """Persist a bounded, replayable run summary beside the graph state."""
        prior = dict(prior_manifest) if isinstance(prior_manifest, Mapping) else {}
        history = prior.get("approval_history", [])
        approval_history: list[JSONValue] = list(history) if isinstance(history, list) else []
        if resume_value is not None:
            approval_history.append(
                {
                    "decision": str(sanitize_for_manifest(resume_value)),
                    "recorded_at": _utc_now(),
                }
            )
        pending = _interrupt_value(state)
        if pending is not None:
            approval_history.append(
                {
                    "status": "pending",
                    "payload": sanitize_for_manifest(pending),
                    "recorded_at": _utc_now(),
                }
            )
        events = state.get("tool_events", [])
        event_list = events if isinstance(events, list) else []
        failure_kinds = sorted(
            {
                str(event.get("failure_kind"))
                for event in event_list
                if isinstance(event, Mapping) and event.get("failure_kind")
            }
        )
        usage = state.get("usage", {})
        usage_mapping = dict(usage) if isinstance(usage, Mapping) else {}
        manifest: dict[str, JSONValue] = {
            "run_id": run_id,
            "thread_id": thread_id,
            "workspace": str(self.workspace),
            "git_sha": _git_sha(self.workspace),
            "policy_version": _POLICY_VERSION,
            "provider": self.config.provider,
            "model": _effective_model(self.config.model),
            "tool_profile": self.tool_profile,
            "status": _run_status(state, pending is not None),
            "phase": str(state.get("phase") or "unknown"),
            "started_at": prior.get("started_at") or _utc_now(),
            "finished_at": _utc_now(),
            "duration_ms": max(0, int((time.monotonic() - started_monotonic) * 1000)),
            "iterations": int(state.get("iteration", 0)),
            "tool_call_count": len(event_list),
            "failure_kinds": failure_kinds,
            "usage": sanitize_for_manifest(usage_mapping),
            "estimated_cost_usd": _estimated_cost(usage_mapping),
            "approval_history": approval_history,
            "trace_id": trace_id,
        }
        paused = pending is not None
        if not paused:
            await sessions.update_state(
                thread_id, {"run_manifest": manifest}, as_node="record_manifest"
            )
        await self._write_manifest(thread_id, manifest)
        if paused:
            return manifest, None
        snapshot = await sessions.latest_state(thread_id)
        snapshot_config = getattr(snapshot, "config", None)
        checkpoint_config = (
            snapshot_config.get("configurable", {})
            if isinstance(snapshot_config, Mapping)
            else {}
        )
        checkpoint_id = (
            checkpoint_config.get("checkpoint_id")
            if isinstance(checkpoint_config, Mapping)
            else None
        )
        return manifest, checkpoint_id if isinstance(checkpoint_id, str) else None

    def _manifest_path(self, thread_id: str) -> Path:
        digest = hashlib.sha256(thread_id.encode("utf-8")).hexdigest()
        return self.session_dir / "manifests" / f"{digest}.json"

    def _load_manifest(self, thread_id: str) -> dict[str, JSONValue] | None:
        path = self._manifest_path(thread_id)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return cast(dict[str, JSONValue], value) if isinstance(value, dict) else None

    async def _write_manifest(self, thread_id: str, manifest: Mapping[str, JSONValue]) -> None:
        path = self._manifest_path(thread_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(manifest, ensure_ascii=False, default=str, sort_keys=True)
        await asyncio.to_thread(path.write_text, payload + "\n", encoding="utf-8")

    async def list_sessions(self) -> list[str]:
        store, _model, _context = self._require_started()
        return await GraphSessionService(None, store).list_threads()

    @property
    def permission_mode(self) -> str:
        return self.config.permission_mode

    @property
    def approval_mode(self) -> str:
        return self.config.approval_mode

    @property
    def execution_mode(self) -> str:
        return self.config.execution_mode

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
    resume_value: object | None = None,
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
            if resume_value is not None:
                if session_id is None:
                    raise ValueError("resume_value requires session_id")
                return await runner.resume_turn(session_id, resume_value)
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


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


def sanitize_for_manifest(value: object) -> JSONValue:
    sanitized = sanitize_for_model_trace_and_persistence(value, max_chars=2_000)
    return cast(JSONValue, sanitized)


def _interrupt_value(state: Mapping[str, object]) -> object | None:
    interrupts = state.get("__interrupt__")
    if not isinstance(interrupts, (list, tuple)) or not interrupts:
        return None
    first = interrupts[0]
    value = getattr(first, "value", first)
    return value if value is not None else {"type": "approval"}


def _run_status(state: Mapping[str, object], paused: bool) -> str:
    if paused:
        return "paused"
    phase = state.get("phase")
    if phase == "done":
        return "completed"
    if phase == "failed":
        return "failed"
    return "running"


def _git_sha(workspace: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(workspace), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=1,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def _estimated_cost(usage: Mapping[str, object]) -> float | None:
    try:
        input_rate = float(os.environ["INSIGHTAGENT_INPUT_COST_PER_1K"])
        output_rate = float(os.environ["INSIGHTAGENT_OUTPUT_COST_PER_1K"])
    except (KeyError, TypeError, ValueError):
        return None
    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)
    if not isinstance(input_tokens, (int, float)) or not isinstance(output_tokens, (int, float)):
        return None
    return round((float(input_tokens) / 1_000 * input_rate) + (float(output_tokens) / 1_000 * output_rate), 8)


def _effective_model(configured: str | None) -> str:
    return configured or os.environ.get("MODEL_ID", "")


__all__ = ["GraphRunner", "RunOutcome", "run_task"]
