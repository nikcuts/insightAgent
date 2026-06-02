from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from insightagent.config import load_runtime_config


class ConfigTests(unittest.TestCase):
    def test_merges_user_project_local_and_cli_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            user_home = root / "user"
            workspace = root / "workspace"
            user_home.mkdir()
            (workspace / ".insightagent").mkdir(parents=True)
            (user_home / "config.json").write_text(
                json.dumps({"model": {"provider": "openai", "name": "small"}, "runtime": {"timeout": 10}}),
                encoding="utf-8",
            )
            (workspace / ".insightagent" / "config.json").write_text(
                json.dumps({"model": {"name": "project-model"}, "permissions": {"mode": "read-only"}}),
                encoding="utf-8",
            )
            (workspace / ".insightagent" / "local.json").write_text(
                json.dumps({"runtime": {"timeout": 99}}),
                encoding="utf-8",
            )

            config = load_runtime_config(
                workspace,
                user_config_home=user_home,
                overrides={"model": "cli-model"},
            )

            self.assertEqual(config.provider, "openai")
            self.assertEqual(config.model, "cli-model")
            self.assertEqual(config.timeout, 99)
            self.assertEqual(config.permission_mode, "read-only")
            self.assertEqual(len(config.loaded_files), 3)


if __name__ == "__main__":
    unittest.main()
