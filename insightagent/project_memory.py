"""Project memory loading for InsightAgent V2."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class MemoryEntry:
    path: Path
    name: str
    content: str


@dataclass(frozen=True)
class ProjectMemory:
    entries: list[MemoryEntry]

    @property
    def has_entries(self) -> bool:
        return bool(self.entries)

    def source_names(self) -> list[str]:
        return [entry.name for entry in self.entries]


def load_project_memory(workspace: Path, filenames: tuple[str, ...]) -> ProjectMemory:
    root = workspace.expanduser().resolve()
    entries: list[MemoryEntry] = []
    for filename in filenames:
        path = root / filename
        if not path.exists():
            continue
        content = path.read_text(encoding="utf-8").strip()
        entries.append(MemoryEntry(path=path, name=filename, content=content))
    return ProjectMemory(entries=entries)
