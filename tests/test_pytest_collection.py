"""Guards the project's pytest collection boundaries."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _collect_pytest() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        check=False,
        capture_output=True,
        text=True,
        cwd=_PROJECT_ROOT,
    )


def test_default_pytest_collection_succeeds_without_collecting_fixture_projects() -> (
    None
):
    result = _collect_pytest()

    assert result.returncode == 0, result.stdout + result.stderr
    assert (
        "tests/fixtures/swe_style/local_calc_addition/test_calc.py" not in result.stdout
    )
    assert "tests/graph/test_workflow.py::test_graph" in result.stdout


def test_collection_uses_project_root_when_parent_cwd_is_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    result = _collect_pytest()

    assert result.returncode == 0, result.stdout + result.stderr
    assert (
        "tests/fixtures/swe_style/local_calc_addition/test_calc.py" not in result.stdout
    )
    assert "tests/graph/test_workflow.py::test_graph" in result.stdout
