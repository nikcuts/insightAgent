"""SQLite checkpoint storage and safe thread-id utilities."""

from __future__ import annotations

import asyncio
import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

import aiosqlite
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver


_THREAD_TABLE = "insightagent_threads"


@dataclass
class CheckpointStore:
    """Own the isolated SQLite connections for LangGraph checkpoints and thread metadata."""

    session_dir: Path
    database_path: Path
    connection: aiosqlite.Connection
    checkpointer: AsyncSqliteSaver
    _checkpoint_connection: aiosqlite.Connection
    index_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    async def close(self) -> None:
        await _close_connections(self.connection, self._checkpoint_connection)


async def open_checkpointer(session_dir: str | Path) -> CheckpointStore:
    """Open and initialize one session database and its LangGraph saver."""
    root = Path(session_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    (root / "locks").mkdir(exist_ok=True)
    database_path = root / "checkpoints.sqlite3"
    checkpoint_connection: aiosqlite.Connection | None = None
    connection: aiosqlite.Connection | None = None
    try:
        checkpoint_connection = await aiosqlite.connect(database_path)
        connection = await aiosqlite.connect(database_path)
        for candidate in (checkpoint_connection, connection):
            await candidate.execute("PRAGMA journal_mode=WAL")
            await candidate.execute("PRAGMA busy_timeout=5000")
        checkpointer = AsyncSqliteSaver(checkpoint_connection)
        await checkpointer.setup()
        await connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {_THREAD_TABLE} (
                thread_id TEXT PRIMARY KEY,
                workspace TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        await connection.commit()
    except BaseException:
        try:
            await _close_connections(connection, checkpoint_connection)
        except BaseException:
            # 创建失败的根因对调用方更有价值；已逐一尝试关闭所有已获取连接。
            pass
        raise
    return CheckpointStore(root, database_path, connection, checkpointer, checkpoint_connection)


async def _close_connections(*connections: aiosqlite.Connection | None) -> None:
    """Try every connection close and re-raise the first close failure afterwards."""
    first_error: BaseException | None = None
    for connection in connections:
        if connection is None:
            continue
        try:
            await connection.close()
        except BaseException as error:
            if first_error is None:
                first_error = error
    if first_error is not None:
        raise first_error


def validate_thread_id(thread_id: str) -> str:
    """Validate an opaque public thread id before it reaches paths or SQLite."""
    if not isinstance(thread_id, str) or not 1 <= len(thread_id) <= 255:
        raise ValueError("thread_id must contain between 1 and 255 characters")
    if thread_id != thread_id.strip() or "\x00" in thread_id:
        raise ValueError("thread_id must not contain whitespace padding or NUL")
    separators = {"/", "\\", os.sep}
    if os.altsep:
        separators.add(os.altsep)
    if any(separator in thread_id for separator in separators) or Path(thread_id).is_absolute():
        raise ValueError("thread_id must not contain path separators")
    return thread_id


def thread_lock_path(session_dir: str | Path, thread_id: str) -> Path:
    """Return a fixed lock path that cannot escape the session directory."""
    validated = validate_thread_id(thread_id)
    root = Path(session_dir).expanduser().resolve()
    digest = hashlib.sha256(validated.encode("utf-8")).hexdigest()
    return root / "locks" / f"{digest}.lock"


__all__ = [
    "CheckpointStore",
    "open_checkpointer",
    "thread_lock_path",
    "validate_thread_id",
]
