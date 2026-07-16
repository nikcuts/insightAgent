from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_installed_cli_uses_graph_runtime_with_test_only_fake_model(tmp_path: Path) -> None:
    env = {
        "PATH": os.environ["PATH"],
        "INSIGHTAGENT_FAKE_MODEL": "write-and-verify",
        "PYTEST_CURRENT_TEST": "tests/test_install_smoke.py::test_installed_cli_uses_graph_runtime_with_test_only_fake_model",
    }
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "insightagent.cli.run_task",
            "--no-trace",
            "--workspace",
            str(tmp_path),
            "--task",
            "创建 hello.py",
        ],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert (tmp_path / "hello.py").is_file()


def test_fake_model_environment_switch_is_rejected_outside_pytest(monkeypatch) -> None:
    from insightagent.config import RuntimeConfig
    from insightagent.graph.models import ModelConfigurationError, build_chat_model

    monkeypatch.setenv("INSIGHTAGENT_FAKE_MODEL", "write-and-verify")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    try:
        build_chat_model(RuntimeConfig(), [])
    except ModelConfigurationError as error:
        assert "INSIGHTAGENT_FAKE_MODEL" in str(error)
    else:
        raise AssertionError("production fake model switch must be rejected")
