from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from insightagent.cli import smoke


class SmokeCliTests(unittest.TestCase):
    def test_main_loads_unified_dotenv_before_calling_graph_runner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / ".env").write_text(
                "API_KEY=from-dotenv\nBASE_URL=https://example.test/v1\nMODEL_ID=test-model\n",
                encoding="utf-8",
            )
            captured: dict[str, object] = {}

            def fake_run_task(**kwargs: object) -> SimpleNamespace:
                captured.update(kwargs)
                captured["api_key"] = os.environ.get("API_KEY")
                captured["base_url"] = os.environ.get("BASE_URL")
                captured["model_id"] = os.environ.get("MODEL_ID")
                return SimpleNamespace(final_answer="pong", state={"phase": "done"})

            saved = {name: os.environ.pop(name, None) for name in ("API_KEY", "BASE_URL", "MODEL_ID")}
            old_cwd = Path.cwd()
            try:
                os.chdir(workspace)
                with (
                    patch.object(sys, "argv", ["smoke", "--provider", "siliconflow"]),
                    patch.object(smoke, "run_task", side_effect=fake_run_task),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    smoke.main()
            finally:
                os.chdir(old_cwd)
                for name, value in saved.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value

        self.assertEqual(captured["config"].provider, "siliconflow")  # type: ignore[union-attr]
        self.assertEqual(captured["api_key"], "from-dotenv")
        self.assertEqual(captured["base_url"], "https://example.test/v1")
        self.assertEqual(captured["model_id"], "test-model")
        self.assertEqual(os.environ.get("API_KEY"), saved["API_KEY"])

    def test_main_exits_nonzero_when_graph_turn_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            with (
                patch.object(sys, "argv", ["smoke", "--workspace", str(workspace)]),
                patch.object(
                    smoke,
                    "run_task",
                    return_value=SimpleNamespace(
                        final_answer="仓库修复任务需要实际检查、修改和验证。",
                        state={"phase": "failed"},
                    ),
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                with self.assertRaises(SystemExit) as raised:
                    smoke.main()

        self.assertEqual(raised.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
