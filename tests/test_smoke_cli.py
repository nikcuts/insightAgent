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
    def test_main_loads_dotenv_before_building_siliconflow_client(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / ".env").write_text("SILICONFLOW_API_KEY=from-dotenv\n", encoding="utf-8")
            captured: dict[str, object] = {}

            class FakeAgent:
                def __init__(self, client: object, tools: object | None = None) -> None:
                    captured["client"] = client
                    captured["tools"] = tools

                def run_turn(self, prompt: str) -> SimpleNamespace:
                    captured["prompt"] = prompt
                    return SimpleNamespace(content="pong")

            def fake_client(**kwargs: object) -> object:
                captured.update(kwargs)
                return object()

            old_key = os.environ.pop("SILICONFLOW_API_KEY", None)
            old_cwd = Path.cwd()
            try:
                os.chdir(workspace)
                with (
                    patch.object(sys, "argv", ["smoke", "--provider", "siliconflow"]),
                    patch.object(smoke, "OpenAICompatibleClient", side_effect=fake_client),
                    patch.object(smoke, "CodeAgent", FakeAgent),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    smoke.main()
            finally:
                os.chdir(old_cwd)
                if old_key is None:
                    os.environ.pop("SILICONFLOW_API_KEY", None)
                else:
                    os.environ["SILICONFLOW_API_KEY"] = old_key

        self.assertEqual(captured["api_key"], "from-dotenv")


if __name__ == "__main__":
    unittest.main()
