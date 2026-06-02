"""Context and memory management for InsightAgent V5.0."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .messages import Message


MEMORY_FILENAMES = (".codeagent.md", "MEMORY.md")


@dataclass(frozen=True)
class ProjectMemory:
    sections: list[tuple[str, str]]

    @property
    def is_empty(self) -> bool:
        return not self.sections

    def render(self) -> str:
        rendered = []
        for filename, content in self.sections:
            rendered.append(f"### {filename}\n{content.strip()}")
        return "\n\n".join(rendered)


@dataclass(frozen=True)
class TruncationResult:
    content: str
    truncated: bool
    original_chars: int
    omitted_chars: int = 0


@dataclass(frozen=True)
class CompactionResult:
    messages: list[Message]
    compacted_count: int


def load_project_memory(workspace: str | Path, filenames: tuple[str, ...] = MEMORY_FILENAMES) -> ProjectMemory:
    root = Path(workspace)
    sections = []
    for filename in filenames:
        path = root / filename
        if path.is_file():
            content = path.read_text(encoding="utf-8").strip()
            if content:
                sections.append((filename, content))
    return ProjectMemory(sections=sections)


def build_system_prompt(base_prompt: str, project_memory: ProjectMemory | None = None) -> str:
    if project_memory is None or project_memory.is_empty:
        return base_prompt
    return (
        f"{base_prompt.rstrip()}\n\n"
        "Project memory follows. Treat it as persistent project guidance and prefer it over generic defaults.\n\n"
        f"{project_memory.render()}"
    )


class ContextManager:
    """V2.0 context budget controls for tool output and completed turns."""

    def __init__(self, max_tool_output_chars: int = 8000, compact_tool_output_chars: int = 600) -> None:
        self.max_tool_output_chars = max_tool_output_chars
        self.compact_tool_output_chars = compact_tool_output_chars

    def truncate_tool_output(self, content: str) -> TruncationResult:
        if len(content) <= self.max_tool_output_chars:
            return TruncationResult(content=content, truncated=False, original_chars=len(content))

        marker_budget = 160
        body_budget = max(80, self.max_tool_output_chars - marker_budget)
        head_budget = body_budget // 2
        tail_budget = body_budget - head_budget
        head = content[:head_budget]
        tail = content[-tail_budget:]
        omitted = len(content) - len(head) - len(tail)
        truncated = (
            f"{head}\n"
            f"\n[InsightAgent V5 truncated tool output: original_chars={len(content)}, omitted_chars={omitted}]\n\n"
            f"{tail}"
        )
        return TruncationResult(
            content=truncated,
            truncated=True,
            original_chars=len(content),
            omitted_chars=omitted,
        )

    def compact_after_completion(self, messages: list[Message]) -> CompactionResult:
        compacted = []
        compacted_count = 0
        for message in messages:
            if message.role == "tool" and len(message.content) > self.compact_tool_output_chars:
                compacted_count += 1
                compacted.append(
                    Message(
                        role=message.role,
                        content=self._compact_tool_content(message.content),
                        tool_call_id=message.tool_call_id,
                        is_error=message.is_error,
                    )
                )
            else:
                compacted.append(message)
        return CompactionResult(messages=compacted, compacted_count=compacted_count)

    def _compact_tool_content(self, content: str) -> str:
        preview_budget = max(80, self.compact_tool_output_chars - 140)
        preview = content[:preview_budget]
        return (
            f"{preview}\n"
            f"\n[InsightAgent V5 compacted completed tool output: original_chars={len(content)}]"
        )
