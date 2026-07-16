from __future__ import annotations

from pathlib import Path

from insightagent.graph.project_memory import load_project_memory


def test_load_project_memory_keeps_known_files_in_stable_order(tmp_path: Path) -> None:
    (tmp_path / ".codeagent.md").write_text("优先读取测试。\n", encoding="utf-8")
    (tmp_path / "MEMORY.md").write_text("\n保留现有接口。\n", encoding="utf-8")

    memory = load_project_memory(tmp_path)

    assert memory.filenames == (".codeagent.md", "MEMORY.md")
    assert memory.render() == "### .codeagent.md\n优先读取测试。\n\n### MEMORY.md\n保留现有接口。"


def test_load_project_memory_ignores_empty_and_missing_files(tmp_path: Path) -> None:
    (tmp_path / ".codeagent.md").write_text(" \n", encoding="utf-8")

    memory = load_project_memory(tmp_path)

    assert memory.is_empty is True
    assert memory.filenames == ()
    assert memory.render() == ""
