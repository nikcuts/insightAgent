from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from insightagent.config import load_dotenv_files, load_runtime_config


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

    def test_load_dotenv_files_sets_missing_values_without_overriding_existing_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            (root / ".env").write_text("ROOT_ONLY=root\nSHARED=root-value\n", encoding="utf-8")
            (workspace / ".env").write_text("WORKSPACE_ONLY=workspace\nSHARED=workspace-value\n", encoding="utf-8")
            old_values = {name: os.environ.get(name) for name in ["ROOT_ONLY", "WORKSPACE_ONLY", "SHARED"]}
            os.environ["SHARED"] = "already-set"
            for name in ["ROOT_ONLY", "WORKSPACE_ONLY"]:
                os.environ.pop(name, None)
            try:
                loaded = load_dotenv_files(workspace, start_dir=root)

                self.assertEqual(os.environ["ROOT_ONLY"], "root")
                self.assertEqual(os.environ["WORKSPACE_ONLY"], "workspace")
                self.assertEqual(os.environ["SHARED"], "already-set")
                self.assertEqual(len(loaded), 2)
            finally:
                for name, value in old_values.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value


if __name__ == "__main__":
    unittest.main()
