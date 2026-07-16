from __future__ import annotations

import asyncio
import json
import multiprocessing
import queue
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from filelock import AcquireReturnProxy
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, START, StateGraph

from insightagent.graph.state import AgentState


class _Cursor:
    def __init__(self, row: tuple[str] | None = None, rows: list[tuple[str]] | None = None) -> None:
        self._row = row
        self._rows = rows or []

    async def fetchone(self) -> tuple[str] | None:
        return self._row

    async def fetchall(self) -> list[tuple[str]]:
        return self._rows

    async def close(self) -> None:
        return None


class _CancellationWindowConnection:
    """模拟 SQLite 已执行 BEGIN、但调用方尚未收到返回值的窗口。"""

    def __init__(self) -> None:
        self.rows: dict[str, str] = {}
        self.in_transaction = False
        self.rollback_calls = 0
        self.begin_applied = asyncio.Event()
        self.allow_begin_return = asyncio.Event()
        self.rollback_applied = asyncio.Event()
        self.allow_rollback_return = asyncio.Event()
        self.fail_select = False
        self.block_rollback = False

    async def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> _Cursor:
        normalized = " ".join(sql.split()).upper()
        if normalized == "BEGIN IMMEDIATE":
            if self.in_transaction:
                raise RuntimeError("nested transaction")
            self.in_transaction = True
            self.begin_applied.set()
            await self.allow_begin_return.wait()
            return _Cursor()
        if normalized.startswith("SELECT WORKSPACE"):
            if self.fail_select and self.in_transaction:
                raise RuntimeError("select failed")
            thread_id = str(parameters[0])
            workspace = self.rows.get(thread_id)
            return _Cursor((workspace,) if workspace is not None else None)
        if normalized.startswith("INSERT INTO INSIGHTAGENT_THREADS"):
            self.rows[str(parameters[0])] = str(parameters[1])
            return _Cursor()
        if normalized.startswith("UPDATE INSIGHTAGENT_THREADS"):
            return _Cursor()
        if normalized.startswith("SELECT THREAD_ID"):
            return _Cursor(rows=[(thread_id,) for thread_id in sorted(self.rows)])
        raise AssertionError(f"unexpected SQL: {sql}")

    async def commit(self) -> None:
        self.in_transaction = False

    async def rollback(self) -> None:
        self.rollback_calls += 1
        self.in_transaction = False
        self.rollback_applied.set()
        if self.block_rollback:
            await self.allow_rollback_return.wait()

    async def close(self) -> None:
        return None


class _ReadableGraph:
    async def ainvoke(self, *_args: object, **_kwargs: object) -> AgentState:
        return {"task": "已处理"}

    async def aget_state(self, *_args: object, **_kwargs: object):
        return type(
            "Snapshot",
            (),
            {
                "values": {"task": "已处理"},
                "config": {"configurable": {"checkpoint_id": "checkpoint-1"}},
            },
        )()


class _CloseTrackingConnection:
    def __init__(self, *, fail_close: bool = False) -> None:
        self.fail_close = fail_close
        self.closed = False

    async def close(self) -> None:
        self.closed = True
        if self.fail_close:
            raise RuntimeError("close failed")


def _cross_process_graph(checkpointer: Any, entered: Any, release: Any, *, block: bool):
    async def finish_turn(state: AgentState) -> AgentState:
        del state
        entered.set()
        if block:
            await asyncio.to_thread(release.wait)
        return {"messages": [AIMessage(content="已处理")], "phase": "done"}

    workflow = StateGraph(AgentState)
    workflow.add_node("finish_turn", finish_turn)
    workflow.add_edge(START, "finish_turn")
    workflow.add_edge("finish_turn", END)
    return workflow.compile(checkpointer=checkpointer)


def _cross_process_turn_worker(
    session_dir: str,
    workspace: str,
    task: str,
    entered: Any,
    release: Any,
    lock_attempts: Any | None,
    result_queue: Any,
    *,
    block: bool,
) -> None:
    from insightagent.graph.checkpoints import open_checkpointer
    from insightagent.graph import sessions as session_module

    original_file_lock = session_module.FileLock
    if lock_attempts is not None:

        class _ReportingFileLock(original_file_lock):
            def acquire(
                self,
                timeout: float | None = None,
                poll_interval: float | None = None,
                *,
                poll_intervall: float | None = None,
                blocking: bool | None = None,
                cancel_check: Callable[[], bool] | None = None,
            ) -> AcquireReturnProxy:
                try:
                    result = super().acquire(
                        timeout=timeout,
                        poll_interval=poll_interval,
                        poll_intervall=poll_intervall,
                        blocking=blocking,
                        cancel_check=cancel_check,
                    )
                except BaseException:
                    if not getattr(self, "_reported", False):
                        self._reported = True
                        lock_attempts.put("blocked")
                    raise
                if not getattr(self, "_reported", False):
                    self._reported = True
                    lock_attempts.put("acquired")
                return result

        session_module.FileLock = _ReportingFileLock

    async def scenario() -> None:
        store = await open_checkpointer(session_dir)
        try:
            sessions = session_module.GraphSessionService(
                _cross_process_graph(store.checkpointer, entered, release, block=block), store
            )
            turn = await sessions.start_turn(
                "shared-thread", workspace, task, deadline_monotonic=None
            )
            human_messages = [
                message.content
                for message in turn.state.get("messages", [])
                if message.type == "human"
            ]
            result_queue.put(("result", task, human_messages, await sessions.list_threads()))
        finally:
            await store.close()

    try:
        asyncio.run(scenario())
    except BaseException as error:
        result_queue.put(("error", task, f"{type(error).__name__}: {error}"))
    finally:
        session_module.FileLock = original_file_lock


def _test_graph(checkpointer: Any):
    def finish_turn(state: AgentState) -> AgentState:
        return {
            "messages": [AIMessage(content="已处理")],
            "phase": "done",
            "iteration": int(state.get("iteration", 0)) + 1,
        }

    workflow = StateGraph(AgentState)
    workflow.add_node("finish_turn", finish_turn)
    workflow.add_edge(START, "finish_turn")
    workflow.add_edge("finish_turn", END)
    return workflow.compile(checkpointer=checkpointer)


def test_session_service_preserves_history_and_exports_transcript(tmp_path: Path) -> None:
    from insightagent.graph.checkpoints import open_checkpointer
    from insightagent.graph.sessions import GraphSessionService

    async def scenario() -> None:
        store = await open_checkpointer(tmp_path / "sessions")
        try:
            graph = _test_graph(store.checkpointer)
            sessions = GraphSessionService(graph, store)
            first = await sessions.start_turn(
                "thread-1", tmp_path, "创建 a.py", deadline_monotonic=None
            )
            second = await sessions.start_turn(
                "thread-1", tmp_path, "检查 a.py", deadline_monotonic=None
            )

            assert first.thread_id == second.thread_id == "thread-1"
            assert first.checkpoint_id is not None
            assert second.checkpoint_id is not None
            assert [
                message.content for message in second.state.get("messages", []) if message.type == "human"
            ] == [
                "创建 a.py",
                "检查 a.py",
            ]
            assert second.state.get("changed_files") == []
            assert await sessions.list_threads() == ["thread-1"]

            latest = await sessions.latest_state("thread-1")
            checkpoint_id = latest.config["configurable"]["checkpoint_id"]
            selected = await sessions.checkpoint_state("thread-1", checkpoint_id)
            assert selected.values["task"] == "检查 a.py"
            assert len(await sessions.history("thread-1")) >= 2

            destination = tmp_path / "transcript.md"
            assert await sessions.export_markdown("thread-1", destination) == destination
            transcript = destination.read_text(encoding="utf-8")
            assert transcript.startswith("# InsightAgent 会话转录")
            assert "创建 a.py" in transcript
            assert "检查 a.py" in transcript
        finally:
            await store.close()

    asyncio.run(scenario())


def test_rendered_transcript_redacts_message_content_and_tool_event_secrets() -> None:
    from insightagent.graph.sessions import _render_transcript

    secret = "canary-secret-123"
    transcript = _render_transcript(
        "thread-1",
        {
            "messages": [HumanMessage(content=f"api_key={secret}")],
            "tool_events": [
                {
                    "headers": {"Authorization": f"Bearer {secret}"},
                    "content": f"token={secret}",
                }
            ],
        },
    )

    assert secret not in transcript
    assert "***REDACTED***" in transcript


def test_rendered_transcript_redacts_thread_id_secrets() -> None:
    from insightagent.graph.sessions import _render_transcript

    secret = "thread-canary-secret-123"
    transcript = _render_transcript(f"access_token={secret}", {})

    assert secret not in transcript
    assert "***REDACTED***" in transcript


def test_rendered_transcript_redacts_json_and_state_summary_secrets() -> None:
    from insightagent.graph.sessions import _render_transcript

    secret = "canary-secret-123"
    access_secret = "access-canary-secret-123"
    transcript = _render_transcript(
        "thread-1",
        {
            "messages": [HumanMessage(content=json.dumps({"api_key": secret, "note": "保留"}))],
            "phase": f"access_token={access_secret}",
            "verification_command": f"pytest access_token={access_secret} -q",
            "changed_files": [f"src/access_token={access_secret}.py", "src/keep.py"],
            "verification_attempts": [
                {
                    "command": f"curl -H 'access_token={access_secret}' https://example.test",
                    "exit_code": 1,
                }
            ],
            "tool_events": [
                {
                    "content": (
                        json.dumps({"token": secret, "result": "保留"})
                        + f" access_token={access_secret}"
                    )
                }
            ],
        },
    )

    assert secret not in transcript
    assert access_secret not in transcript
    assert "***REDACTED***" in transcript
    assert "src/keep.py" in transcript
    assert "保留" in transcript


def test_rendered_transcript_redacts_inline_credential_cookie_and_client_secret() -> None:
    from insightagent.graph.sessions import _render_transcript

    secrets = {
        "credential": "credential-canary-123",
        "cookie": "cookie-canary-123",
        "client_secret": "client-secret-canary-123",
    }
    transcript = _render_transcript(
        f"client_secret={secrets['client_secret']}",
        {
            "messages": [HumanMessage(content=f"credential={secrets['credential']}")],
            "phase": f"cookie={secrets['cookie']}",
            "verification_command": f"pytest --credential {secrets['credential']} -q",
            "changed_files": [f"src/cookie={secrets['cookie']}.py"],
            "verification_attempts": [
                {"command": f"check client_secret={secrets['client_secret']}", "exit_code": 1}
            ],
            "tool_events": [{"content": f"credential={secrets['credential']}"}],
        },
    )

    assert all(secret not in transcript for secret in secrets.values())
    assert "***REDACTED***" in transcript


def test_rendered_transcript_redacts_every_cookie_value() -> None:
    from insightagent.graph.sessions import _render_transcript

    session = "session-canary-123"
    csrf = "csrf-canary-123"
    transcript = _render_transcript(
        "thread-1",
        {
            "messages": [HumanMessage(content=f"Cookie: session={session}; csrf={csrf}")],
            "verification_command": f'curl --cookie "session={session}; csrf={csrf}"',
        },
    )

    assert session not in transcript
    assert csrf not in transcript
    assert "***REDACTED***" in transcript


def test_rendered_transcript_redacts_short_option_cookie_value() -> None:
    from insightagent.graph.sessions import _render_transcript

    secret = "short-cookie-canary-123"
    transcript = _render_transcript(
        "thread-1",
        {"verification_command": f"curl -b'session={secret}; csrf=other'"},
    )

    assert secret not in transcript
    assert "***REDACTED***" in transcript


def test_session_service_rejects_invalid_or_rebound_thread_ids(tmp_path: Path) -> None:
    from insightagent.graph.checkpoints import open_checkpointer, thread_lock_path, validate_thread_id
    from insightagent.graph.sessions import GraphSessionService, SessionWorkspaceMismatch

    for invalid in ("", "../escape", "/absolute", "nested/thread", "nested\\thread", "x" * 256):
        with pytest.raises(ValueError):
            validate_thread_id(invalid)

    lock_path = thread_lock_path(tmp_path / "sessions", "thread-1")
    assert lock_path.parent == (tmp_path / "sessions" / "locks").resolve()
    assert lock_path.name.endswith(".lock")

    async def scenario() -> None:
        store = await open_checkpointer(tmp_path / "sessions")
        try:
            graph = _test_graph(store.checkpointer)
            sessions = GraphSessionService(graph, store)
            await sessions.start_turn("thread-1", tmp_path, "创建 a.py", deadline_monotonic=None)
            other_workspace = tmp_path / "other"
            other_workspace.mkdir()
            with pytest.raises(SessionWorkspaceMismatch):
                await sessions.start_turn(
                    "thread-1", other_workspace, "检查 a.py", deadline_monotonic=None
                )
        finally:
            await store.close()

    asyncio.run(scenario())


def test_session_service_keeps_only_readable_partial_state_after_graph_failure(
    tmp_path: Path,
) -> None:
    from insightagent.graph.checkpoints import open_checkpointer
    from insightagent.graph.sessions import GraphSessionService

    def fail_turn(state: AgentState) -> AgentState:
        del state
        raise RuntimeError("graph failed before checkpoint")

    async def scenario() -> None:
        store = await open_checkpointer(tmp_path / "sessions")
        try:
            workflow = StateGraph(AgentState)
            workflow.add_node("fail_turn", fail_turn)
            workflow.add_edge(START, "fail_turn")
            workflow.add_edge("fail_turn", END)
            sessions = GraphSessionService(workflow.compile(checkpointer=store.checkpointer), store)

            with pytest.raises(RuntimeError, match="graph failed before checkpoint"):
                await sessions.start_turn("thread-1", tmp_path, "失败任务", deadline_monotonic=None)

            assert await sessions.list_threads() == ["thread-1"]
            assert (await sessions.latest_state("thread-1")).values["task"] == "失败任务"
        finally:
            await store.close()

    asyncio.run(scenario())


def test_session_service_serializes_concurrent_turns_for_the_same_thread(tmp_path: Path) -> None:
    from insightagent.graph.checkpoints import open_checkpointer
    from insightagent.graph.sessions import GraphSessionService

    active = 0
    maximum_active = 0

    async def finish_turn(state: AgentState) -> AgentState:
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        try:
            await asyncio.sleep(0.05)
        finally:
            active -= 1
        return {"messages": [AIMessage(content="已处理")], "phase": "done"}

    async def scenario() -> None:
        store = await open_checkpointer(tmp_path / "sessions")
        try:
            workflow = StateGraph(AgentState)
            workflow.add_node("finish_turn", finish_turn)
            workflow.add_edge(START, "finish_turn")
            workflow.add_edge("finish_turn", END)
            sessions = GraphSessionService(workflow.compile(checkpointer=store.checkpointer), store)

            first = asyncio.create_task(
                sessions.start_turn("thread-1", tmp_path, "第一轮", deadline_monotonic=None)
            )
            await asyncio.sleep(0.01)
            second = asyncio.create_task(
                sessions.start_turn("thread-1", tmp_path, "第二轮", deadline_monotonic=None)
            )
            await asyncio.gather(first, second)

            assert maximum_active == 1
            state = await sessions.latest_state("thread-1")
            assert [message.content for message in state.values["messages"] if message.type == "human"] == [
                "第一轮",
                "第二轮",
            ]
        finally:
            await store.close()

    asyncio.run(scenario())


def test_session_service_does_not_index_a_thread_without_a_readable_checkpoint(
    tmp_path: Path,
) -> None:
    from insightagent.graph.checkpoints import open_checkpointer
    from insightagent.graph.sessions import GraphSessionService

    class _NoCheckpointGraph:
        async def ainvoke(self, *_args: object, **_kwargs: object) -> AgentState:
            raise RuntimeError("checkpoint write failed")

        async def aget_state(self, *_args: object, **_kwargs: object):
            return type("Snapshot", (), {"values": {}})()

    async def scenario() -> None:
        store = await open_checkpointer(tmp_path / "sessions")
        try:
            sessions = GraphSessionService(_NoCheckpointGraph(), store)
            with pytest.raises(RuntimeError, match="checkpoint write failed"):
                await sessions.start_turn("thread-1", tmp_path, "失败任务", deadline_monotonic=None)
            assert await sessions.list_threads() == []
        finally:
            await store.close()

    asyncio.run(scenario())


def test_session_service_allows_distinct_threads_to_progress_concurrently(tmp_path: Path) -> None:
    from insightagent.graph.checkpoints import open_checkpointer
    from insightagent.graph.sessions import GraphSessionService

    active = 0
    maximum_active = 0

    async def finish_turn(state: AgentState) -> AgentState:
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        try:
            await asyncio.sleep(0.05)
        finally:
            active -= 1
        return {"messages": [AIMessage(content="已处理")], "phase": "done"}

    async def scenario() -> None:
        store = await open_checkpointer(tmp_path / "sessions")
        try:
            workflow = StateGraph(AgentState)
            workflow.add_node("finish_turn", finish_turn)
            workflow.add_edge(START, "finish_turn")
            workflow.add_edge("finish_turn", END)
            sessions = GraphSessionService(workflow.compile(checkpointer=store.checkpointer), store)

            await asyncio.gather(
                sessions.start_turn("thread-a", tmp_path, "第一轮", deadline_monotonic=None),
                sessions.start_turn("thread-b", tmp_path, "第二轮", deadline_monotonic=None),
            )

            assert maximum_active == 2
            assert await sessions.list_threads() == ["thread-a", "thread-b"]
        finally:
            await store.close()

    asyncio.run(scenario())


def test_session_service_rolls_back_when_cancelled_after_begin_is_applied(tmp_path: Path) -> None:
    from insightagent.graph.checkpoints import CheckpointStore
    from insightagent.graph.sessions import GraphSessionService

    async def scenario() -> None:
        session_dir = tmp_path / "sessions"
        (session_dir / "locks").mkdir(parents=True)
        connection = _CancellationWindowConnection()
        store = CheckpointStore(
            session_dir=session_dir,
            database_path=session_dir / "checkpoints.sqlite3",
            connection=connection,  # type: ignore[arg-type]
            checkpointer=object(),  # type: ignore[arg-type]
            _checkpoint_connection=_CancellationWindowConnection(),  # type: ignore[arg-type]
        )
        sessions = GraphSessionService(_ReadableGraph(), store)
        try:
            cancelled_turn = asyncio.create_task(
                sessions.start_turn("thread-1", tmp_path, "第一轮", deadline_monotonic=None)
            )
            await connection.begin_applied.wait()
            cancelled_turn.cancel()
            connection.allow_begin_return.set()
            with pytest.raises(asyncio.CancelledError):
                await cancelled_turn

            assert connection.in_transaction is False
            assert connection.rollback_calls == 1

            completed = await sessions.start_turn(
                "thread-1", tmp_path, "第二轮", deadline_monotonic=None
            )
            assert completed.checkpoint_id == "checkpoint-1"
            assert await sessions.list_threads() == ["thread-1"]
        finally:
            await store.close()

    asyncio.run(scenario())


def test_session_service_propagates_cancellation_after_rollback_completes(tmp_path: Path) -> None:
    from insightagent.graph.checkpoints import CheckpointStore
    from insightagent.graph.sessions import GraphSessionService

    async def scenario() -> None:
        session_dir = tmp_path / "sessions"
        (session_dir / "locks").mkdir(parents=True)
        connection = _CancellationWindowConnection()
        connection.allow_begin_return.set()
        connection.fail_select = True
        connection.block_rollback = True
        store = CheckpointStore(
            session_dir=session_dir,
            database_path=session_dir / "checkpoints.sqlite3",
            connection=connection,  # type: ignore[arg-type]
            checkpointer=object(),  # type: ignore[arg-type]
            _checkpoint_connection=_CancellationWindowConnection(),  # type: ignore[arg-type]
        )
        sessions = GraphSessionService(_ReadableGraph(), store)
        try:
            turn = asyncio.create_task(
                sessions.start_turn("thread-1", tmp_path, "失败轮次", deadline_monotonic=None)
            )
            await connection.rollback_applied.wait()
            turn.cancel()
            connection.allow_rollback_return.set()
            with pytest.raises(asyncio.CancelledError):
                await turn

            assert connection.in_transaction is False
            assert connection.rollback_calls == 1
        finally:
            await store.close()

    asyncio.run(scenario())


def test_checkpoint_store_closes_both_connections_when_one_close_fails(tmp_path: Path) -> None:
    from insightagent.graph.checkpoints import CheckpointStore

    async def scenario() -> None:
        index_connection = _CloseTrackingConnection(fail_close=True)
        checkpoint_connection = _CloseTrackingConnection()
        store = CheckpointStore(
            session_dir=tmp_path,
            database_path=tmp_path / "checkpoints.sqlite3",
            connection=index_connection,  # type: ignore[arg-type]
            checkpointer=object(),  # type: ignore[arg-type]
            _checkpoint_connection=checkpoint_connection,  # type: ignore[arg-type]
        )

        with pytest.raises(RuntimeError, match="close failed"):
            await store.close()

        assert index_connection.closed is True
        assert checkpoint_connection.closed is True

    asyncio.run(scenario())


def test_open_checkpointer_closes_first_connection_when_second_creation_fails(
    monkeypatch, tmp_path: Path
) -> None:
    from insightagent.graph import checkpoints

    async def scenario() -> None:
        first_connection = _CloseTrackingConnection()
        calls = 0

        async def fake_connect(_path: object) -> _CloseTrackingConnection:
            nonlocal calls
            calls += 1
            if calls == 1:
                return first_connection
            raise RuntimeError("second connection failed")

        monkeypatch.setattr(checkpoints.aiosqlite, "connect", fake_connect)
        with pytest.raises(RuntimeError, match="second connection failed"):
            await checkpoints.open_checkpointer(tmp_path / "sessions")

        assert first_connection.closed is True

    asyncio.run(scenario())


def test_session_service_serializes_the_same_thread_across_processes(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    tests_root = Path(__file__).resolve().parents[1]
    if str(tests_root) not in sys.path:
        sys.path.insert(0, str(tests_root))
    session_dir = tmp_path / "sessions"
    first_entered = context.Event()
    second_entered = context.Event()
    release_first = context.Event()
    release_second = context.Event()
    lock_attempts = context.Queue()
    results = context.Queue()
    first = context.Process(
        target=_cross_process_turn_worker,
        args=(
            str(session_dir),
            str(tmp_path),
            "第一轮",
            first_entered,
            release_first,
            None,
            results,
        ),
        kwargs={"block": True},
    )
    second = context.Process(
        target=_cross_process_turn_worker,
        args=(
            str(session_dir),
            str(tmp_path),
            "第二轮",
            second_entered,
            release_second,
            lock_attempts,
            results,
        ),
        kwargs={"block": False},
    )
    try:
        first.start()
        assert first_entered.wait(timeout=5)
        second.start()
        assert lock_attempts.get(timeout=5) == "blocked"
        assert second_entered.is_set() is False

        release_first.set()
        first.join(timeout=10)
        second.join(timeout=10)
        assert first.exitcode == second.exitcode == 0

        entries = [results.get(timeout=5), results.get(timeout=5)]
        assert not [entry for entry in entries if entry[0] == "error"]
        second_result = next(entry for entry in entries if entry[1] == "第二轮")
        assert second_result[2] == ["第一轮", "第二轮"]
        assert second_result[3] == ["shared-thread"]
    except queue.Empty as error:
        pytest.fail(f"cross-process worker did not report a result: {error}")
    finally:
        release_first.set()
        for process in (first, second):
            if process.pid is not None:
                if process.is_alive():
                    process.terminate()
                process.join(timeout=5)
