"""Project-local guidance loaded by the LangGraph runtime."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


MEMORY_FILENAMES = (".codeagent.md", "MEMORY.md")


@dataclass(frozen=True)
class ProjectMemory:
    """Serializable project guidance kept outside graph checkpoint state."""

    sections: tuple[tuple[str, str], ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.sections

    @property
    def filenames(self) -> tuple[str, ...]:
        return tuple(filename for filename, _content in self.sections)

    def render(self) -> str:
        return "\n\n".join(
            f"### {filename}\n{content}" for filename, content in self.sections
        )


def load_project_memory(
    workspace: str | Path,
    filenames: tuple[str, ...] = MEMORY_FILENAMES,
) -> ProjectMemory:
    """Load non-empty, well-known project guidance files in declared order."""
    root = Path(workspace).expanduser().resolve()
    sections: list[tuple[str, str]] = []
    for filename in filenames:
        path = root / filename
        if not path.is_file():
            continue
        content = path.read_text(encoding="utf-8").strip()
        if content:
            sections.append((filename, content))
    return ProjectMemory(tuple(sections))


__all__ = ["MEMORY_FILENAMES", "ProjectMemory", "load_project_memory"]
