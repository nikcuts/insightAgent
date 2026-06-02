"""Session persistence and transcript export for InsightAgent V5.0."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .messages import Message, ToolCall


@dataclass
class Session:
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    messages: list[Message] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def touch(self) -> None:
        self.updated_at = time.time()


class SessionStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, session_id: str) -> Path:
        return self.root / f"{session_id}.json"

    def create(self, metadata: dict[str, Any] | None = None) -> Session:
        return Session(metadata=metadata or {})

    def save(self, session: Session) -> Path:
        session.touch()
        path = self.path_for(session.session_id)
        path.write_text(json.dumps(_session_to_dict(session), ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def load(self, session_id: str) -> Session:
        path = self.path_for(session_id)
        data = json.loads(path.read_text(encoding="utf-8"))
        return _session_from_dict(data)

    def list_sessions(self) -> list[str]:
        return sorted(path.stem for path in self.root.glob("*.json"))

    def export_markdown(self, session: Session, path: str | Path) -> Path:
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(render_transcript(session), encoding="utf-8")
        return output


def render_transcript(session: Session) -> str:
    lines = [
        f"# InsightAgent Session {session.session_id}",
        "",
        f"- created_at: {session.created_at:.0f}",
        f"- updated_at: {session.updated_at:.0f}",
        "",
    ]
    for index, message in enumerate(session.messages, start=1):
        lines.append(f"## {index}. {message.role}")
        if message.tool_call_id:
            lines.append(f"- tool_call_id: `{message.tool_call_id}`")
        if message.is_error:
            lines.append("- is_error: true")
        if message.tool_calls:
            lines.append("")
            lines.append("Tool calls:")
            for tool_call in message.tool_calls:
                lines.append(f"- `{tool_call.name}` id=`{tool_call.id}` args=`{json.dumps(tool_call.arguments, ensure_ascii=False)}`")
        if message.content:
            lines.append("")
            lines.append(message.content)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _session_to_dict(session: Session) -> dict[str, Any]:
    return {
        "session_id": session.session_id,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
        "metadata": session.metadata,
        "messages": [_message_to_dict(message) for message in session.messages],
    }


def _session_from_dict(data: dict[str, Any]) -> Session:
    return Session(
        session_id=data["session_id"],
        created_at=float(data["created_at"]),
        updated_at=float(data["updated_at"]),
        metadata=dict(data.get("metadata") or {}),
        messages=[_message_from_dict(raw) for raw in data.get("messages", [])],
    )


def _message_to_dict(message: Message) -> dict[str, Any]:
    return {
        "role": message.role,
        "content": message.content,
        "tool_call_id": message.tool_call_id,
        "is_error": message.is_error,
        "tool_calls": [
            {"id": tool_call.id, "name": tool_call.name, "arguments": tool_call.arguments}
            for tool_call in message.tool_calls
        ],
    }


def _message_from_dict(data: dict[str, Any]) -> Message:
    return Message(
        role=data["role"],
        content=data.get("content") or "",
        tool_call_id=data.get("tool_call_id"),
        is_error=bool(data.get("is_error", False)),
        tool_calls=[
            ToolCall(id=raw["id"], name=raw["name"], arguments=dict(raw.get("arguments") or {}))
            for raw in data.get("tool_calls", [])
        ],
    )
