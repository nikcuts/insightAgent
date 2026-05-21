"""System prompt construction for InsightAgent V2."""

from __future__ import annotations

from .project_memory import ProjectMemory


def build_system_prompt(base_prompt: str, project_memory: ProjectMemory) -> str:
    sections = [base_prompt.strip()]
    for entry in project_memory.entries:
        sections.append(f"## Project Memory: {entry.name}\n{entry.content.strip()}")
    return "\n\n".join(section for section in sections if section)
