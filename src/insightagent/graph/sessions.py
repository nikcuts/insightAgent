"""Thread-safe access to persisted LangGraph sessions and transcripts."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Mapping, TypeVar, cast

from filelock import FileLock, Timeout
from langchain_core.messages import BaseMessage
from langgraph.graph import END, START, StateGraph

from insightagent.graph.checkpoints import CheckpointStore, thread_lock_path, validate_thread_id
from insightagent.graph.observability import sanitize_for_model_trace_and_persistence
from insightagent.graph.state import AgentState, new_turn_update


_DatabaseResult = TypeVar("_DatabaseResult")


class SessionWorkspaceMismatch(ValueError):
    """Raised when a persisted thread is reused for another workspace."""


@dataclass(frozen=True)
class SessionTurn:
    """Result of one persisted graph turn."""

    thread_id: str
    state: AgentState
    checkpoint_id: str | None


class GraphSessionService:
    """Coordinates graph turns, SQLite thread metadata and transcript access."""

    def __init__(self, graph: Any, store: CheckpointStore) -> None:
        self._graph = graph
        self._store = store

    async def list_threads(self) -> list[str]:
        async with self._store.index_lock:
            cursor = await self._store.connection.execute(
                "SELECT thread_id FROM insightagent_threads ORDER BY thread_id"
            )
            try:
                rows = await cursor.fetchall()
            finally:
                await cursor.close()
        return [str(row[0]) for row in rows]

    async def start_turn(
        self,
        thread_id: str,
        workspace: str | Path,
        task: str,
        *,
        deadline_monotonic: float | None,
        run_config: Mapping[str, object] | None = None,
    ) -> SessionTurn:
        validated_thread_id = validate_thread_id(thread_id)
        resolved_workspace = str(Path(workspace).expanduser().resolve())
        async with self._thread_lock(validated_thread_id):
            await self._validate_workspace_binding(validated_thread_id, resolved_workspace)
            config = dict(run_config or {})
            configured_values = config.get("configurable", {})
            configurable = (
                dict(configured_values) if isinstance(configured_values, Mapping) else {}
            )
            configurable["thread_id"] = validated_thread_id
            if deadline_monotonic is not None:
                configurable["insightagent_deadline_monotonic"] = deadline_monotonic
            config["configurable"] = configurable
            try:
                state = cast(AgentState, await self._graph.ainvoke(new_turn_update(task), config))
            except BaseException:
                snapshot = await self._readable_snapshot(config)
                if snapshot is not None:
                    await self._record_thread(validated_thread_id, resolved_workspace)
                raise
            snapshot = await self._readable_snapshot(config)
            if snapshot is None:
                raise RuntimeError("graph turn completed without a readable checkpoint")
            await self._record_thread(validated_thread_id, resolved_workspace)
            checkpoint_config = snapshot.config.get("configurable", {})
            checkpoint_id = checkpoint_config.get("checkpoint_id")
            return SessionTurn(
                validated_thread_id,
                state,
                checkpoint_id if isinstance(checkpoint_id, str) else None,
            )

    async def latest_state(self, thread_id: str) -> Any:
        return await self._graph.aget_state(self._thread_config(thread_id))

    async def checkpoint_state(self, thread_id: str, checkpoint_id: str) -> Any:
        if not isinstance(checkpoint_id, str) or not checkpoint_id:
            raise ValueError("checkpoint_id must be a non-empty string")
        config = self._thread_config(thread_id)
        configurable = cast(dict[str, object], config["configurable"])
        configurable["checkpoint_id"] = checkpoint_id
        return await self._graph.aget_state(config)

    async def update_state(
        self,
        thread_id: str,
        values: Mapping[str, object],
        *,
        as_node: str,
    ) -> Any:
        """Serialize a checkpoint state update with concurrent graph turns."""
        validated_thread_id = validate_thread_id(thread_id)
        async with self._thread_lock(validated_thread_id):
            return await self._graph.aupdate_state(
                self._thread_config(validated_thread_id), dict(values), as_node=as_node
            )

    async def history(self, thread_id: str) -> list[Any]:
        return [snapshot async for snapshot in self._graph.aget_state_history(self._thread_config(thread_id))]

    async def export_markdown(
        self,
        thread_id: str,
        destination: str | Path,
        *,
        checkpoint_id: str | None = None,
    ) -> Path:
        validated_thread_id = validate_thread_id(thread_id)
        snapshot = (
            await self.checkpoint_state(validated_thread_id, checkpoint_id)
            if checkpoint_id is not None
            else await self.latest_state(validated_thread_id)
        )
        values = cast(AgentState, snapshot.values)
        destination_path = Path(destination).expanduser()
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(
            destination_path.write_text,
            _render_transcript(validated_thread_id, values),
            encoding="utf-8",
        )
        return destination_path

    @asynccontextmanager
    async def _thread_lock(self, thread_id: str) -> AsyncIterator[None]:
        lock = FileLock(str(thread_lock_path(self._store.session_dir, thread_id)), thread_local=False)
        while True:
            try:
                await asyncio.to_thread(lock.acquire, timeout=0)
                break
            except Timeout:
                await asyncio.sleep(0.05)
        try:
            yield
        finally:
            await asyncio.to_thread(lock.release)

    async def _validate_workspace_binding(self, thread_id: str, workspace: str) -> None:
        async with self._store.index_lock:
            cursor = await self._store.connection.execute(
                "SELECT workspace FROM insightagent_threads WHERE thread_id = ?", (thread_id,)
            )
            try:
                row = await cursor.fetchone()
            finally:
                await cursor.close()
            if row is not None and str(row[0]) != workspace:
                raise SessionWorkspaceMismatch(
                    f"thread_id {thread_id!r} is bound to a different workspace"
                )

    async def _record_thread(self, thread_id: str, workspace: str) -> None:
        now = time.time()
        async with self._store.index_lock:
            transaction_active = True
            try:
                _, cancelled = await _complete_database_operation(
                    self._store.connection.execute("BEGIN IMMEDIATE")
                )
                if cancelled:
                    raise asyncio.CancelledError
                cursor, cancelled = await _complete_database_operation(
                    self._store.connection.execute(
                    "SELECT workspace FROM insightagent_threads WHERE thread_id = ?", (thread_id,)
                    )
                )
                if cancelled:
                    raise asyncio.CancelledError
                try:
                    row, cancelled = await _complete_database_operation(cursor.fetchone())
                    if cancelled:
                        raise asyncio.CancelledError
                finally:
                    _, cancelled = await _complete_database_operation(cursor.close())
                    if cancelled:
                        raise asyncio.CancelledError
                if row is None:
                    _, cancelled = await _complete_database_operation(
                        self._store.connection.execute(
                        """
                        INSERT INTO insightagent_threads (thread_id, workspace, created_at, updated_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (thread_id, workspace, now, now),
                        )
                    )
                    if cancelled:
                        raise asyncio.CancelledError
                elif str(row[0]) != workspace:
                    raise SessionWorkspaceMismatch(
                        f"thread_id {thread_id!r} is bound to a different workspace"
                    )
                else:
                    _, cancelled = await _complete_database_operation(
                        self._store.connection.execute(
                            "UPDATE insightagent_threads SET updated_at = ? WHERE thread_id = ?",
                            (now, thread_id),
                        )
                    )
                    if cancelled:
                        raise asyncio.CancelledError
                _, cancelled = await _complete_database_operation(self._store.connection.commit())
                transaction_active = False
                if cancelled:
                    raise asyncio.CancelledError
            except BaseException as error:
                rollback_cancelled = False
                if transaction_active:
                    rollback_cancelled = await _complete_database_cleanup(
                        self._store.connection.rollback()
                    )
                if rollback_cancelled:
                    raise asyncio.CancelledError from error
                raise

    async def _readable_snapshot(self, config: Mapping[str, object]) -> Any | None:
        try:
            snapshot = await self._graph.aget_state(config)
        except Exception:
            return None
        values = getattr(snapshot, "values", None)
        return snapshot if isinstance(values, Mapping) and values else None

    @staticmethod
    def _thread_config(thread_id: str) -> dict[str, dict[str, str]]:
        return {"configurable": {"thread_id": validate_thread_id(thread_id)}}


def build_checkpoint_reader(checkpointer: Any) -> Any:
    """Compile the minimal valid graph needed to read persisted snapshots."""
    def checkpoint_reader(state: AgentState) -> AgentState:
        del state
        return {}

    workflow = StateGraph(AgentState)
    workflow.add_node("checkpoint_reader", checkpoint_reader)
    workflow.add_edge(START, "checkpoint_reader")
    workflow.add_edge("checkpoint_reader", END)
    return workflow.compile(checkpointer=checkpointer)


async def _complete_database_operation(
    awaitable: Awaitable[_DatabaseResult],
) -> tuple[_DatabaseResult, bool]:
    """Finish an aiosqlite queue item before exposing a caller cancellation."""
    task = asyncio.ensure_future(awaitable)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    return task.result(), cancelled


async def _complete_database_cleanup(awaitable: Awaitable[object]) -> bool:
    """Finish a queued rollback and report whether its caller was cancelled."""
    task = asyncio.ensure_future(awaitable)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    task.result()
    return cancelled


def _render_transcript(thread_id: str, state: AgentState) -> str:
    lines = ["# InsightAgent 会话转录", "", f"- 线程：`{_render_safe_value(thread_id)}`", ""]
    for message in state.get("messages", []):
        lines.extend(_render_message(cast(BaseMessage, message)))
    changed_files = state.get("changed_files", [])
    rendered_changed_files = (
        ", ".join(_render_safe_value(path) for path in changed_files)
        if isinstance(changed_files, list)
        else ""
    )
    lines.extend(
        [
            "## 本轮状态",
            "",
            f"- 阶段：`{_render_safe_value(state.get('phase', 'unknown'))}`",
            f"- 验证命令：`{_render_safe_value(state.get('verification_command') or '未声明')}`",
            f"- 工作区变更：{rendered_changed_files or '无'}",
        ]
    )
    attempts = state.get("verification_attempts", [])
    if attempts:
        lines.extend(["", "## 验证记录", ""])
        for attempt in attempts:
            safe_attempt = sanitize_for_model_trace_and_persistence(attempt)
            values = safe_attempt if isinstance(safe_attempt, Mapping) else {}
            lines.append(
                f"- `{_render_safe_value(values.get('command', ''))}` -> "
                f"exit_code: `{_render_safe_value(values.get('exit_code', ''))}`"
            )
    events = state.get("tool_events", [])
    if events:
        lines.extend(["", "## 工具事件", "", "```json"])
        lines.extend(
            json.dumps(
                sanitize_for_model_trace_and_persistence(event), ensure_ascii=False, default=str
            )
            for event in events
        )
        lines.append("```")
    return "\n".join(lines) + "\n"


def _render_message(message: BaseMessage) -> list[str]:
    role = {"human": "用户", "ai": "助手", "tool": "工具", "system": "系统"}.get(
        message.type, message.type
    )
    return [f"## {role}", "", _render_safe_value(message.content), ""]


def _render_safe_value(value: object) -> str:
    sanitized = sanitize_for_model_trace_and_persistence(value)
    return sanitized if isinstance(sanitized, str) else json.dumps(sanitized, ensure_ascii=False, default=str)


__all__ = [
    "GraphSessionService",
    "SessionTurn",
    "SessionWorkspaceMismatch",
    "build_checkpoint_reader",
]
