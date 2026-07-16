from __future__ import annotations

from pathlib import Path

from insightagent.graph.repository_snapshot import build_repository_snapshot


def test_graph_repository_snapshot_marks_tests_and_excludes_secrets(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text("assert True\n", encoding="utf-8")
    (tmp_path / ".env").write_text("API_KEY=secret\n", encoding="utf-8")

    snapshot = build_repository_snapshot(tmp_path)

    assert "src/app.py" in snapshot.content
    assert "tests/test_app.py [test]" in snapshot.content
    assert ".env" not in snapshot.content
