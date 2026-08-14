from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from insightagent.config import (
    language_directive,
    load_dotenv_files,
    load_runtime_config,
)


class ConfigTests(unittest.TestCase):
    def test_response_language_defaults_to_auto(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = load_runtime_config(Path(directory))

        self.assertEqual(config.response_language, "auto")

    def test_language_override_maps_to_response_language(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = load_runtime_config(
                Path(directory), overrides={"language": "Chinese"}
            )

        self.assertEqual(config.response_language, "Chinese")

    def test_response_language_loaded_from_runtime_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / ".insightagent").mkdir(parents=True)
            (workspace / ".insightagent" / "config.json").write_text(
                json.dumps({"runtime": {"response_language": "Japanese"}}),
                encoding="utf-8",
            )

            config = load_runtime_config(workspace)

        self.assertEqual(config.response_language, "Japanese")

    def test_model_tuning_defaults_preserve_openai_compatible_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = load_runtime_config(Path(directory))

        self.assertEqual(config.temperature, 0.01)
        self.assertEqual(config.top_p, 0.95)
        self.assertEqual(config.max_retries, 3)

    def test_language_directive_auto_mentions_mirroring(self) -> None:
        directive = language_directive("auto")

        self.assertIn("same language", directive)

    def test_language_directive_explicit_pins_language(self) -> None:
        directive = language_directive("Chinese")

        self.assertIn("Chinese", directive)


class ConfigMergeTests(unittest.TestCase):
    def test_merges_user_project_local_and_cli_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            user_home = root / "user"
            workspace = root / "workspace"
            user_home.mkdir()
            (workspace / ".insightagent").mkdir(parents=True)
            (user_home / "config.json").write_text(
                json.dumps(
                    {
                        "model": {"provider": "openai", "name": "small"},
                        "runtime": {"timeout": 10},
                    }
                ),
                encoding="utf-8",
            )
            (workspace / ".insightagent" / "config.json").write_text(
                json.dumps(
                    {
                        "model": {"name": "project-model"},
                        "permissions": {"mode": "read-only"},
                    }
                ),
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

    def test_loads_nested_model_tuning_and_programmatic_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / ".insightagent").mkdir(parents=True)
            (workspace / ".insightagent" / "config.json").write_text(
                json.dumps(
                    {
                        "runtime": {
                            "temperature": 0.4,
                            "top_p": 0.8,
                            "max_retries": 9,
                        }
                    }
                ),
                encoding="utf-8",
            )

            config = load_runtime_config(
                workspace,
                overrides={"temperature": 0.2, "top_p": 0.7, "max_retries": 4},
            )

        self.assertEqual(config.temperature, 0.2)
        self.assertEqual(config.top_p, 0.7)
        self.assertEqual(config.max_retries, 4)

    def test_loads_approval_and_sandbox_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / ".insightagent").mkdir(parents=True)
            (workspace / ".insightagent" / "config.json").write_text(
                json.dumps(
                    {
                        "permissions": {
                            "approval_mode": "interrupt",
                            "execution_mode": "sandbox",
                        },
                        "runtime": {
                            "sandbox_image": "python:3.12-slim",
                            "sandbox_memory_mb": 256,
                            "sandbox_cpus": 0.5,
                            "sandbox_pids_limit": 32,
                        },
                    }
                ),
                encoding="utf-8",
            )

            config = load_runtime_config(workspace)

        self.assertEqual(config.approval_mode, "interrupt")
        self.assertEqual(config.execution_mode, "sandbox")
        self.assertEqual(config.sandbox_image, "python:3.12-slim")
        self.assertEqual(config.sandbox_memory_mb, 256)
        self.assertEqual(config.sandbox_cpus, 0.5)
        self.assertEqual(config.sandbox_pids_limit, 32)

    def test_load_dotenv_files_sets_missing_values_without_overriding_existing_environment(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            (root / ".env").write_text(
                "ROOT_ONLY=root\nSHARED=root-value\n", encoding="utf-8"
            )
            (workspace / ".env").write_text(
                "WORKSPACE_ONLY=workspace\nSHARED=workspace-value\n", encoding="utf-8"
            )
            old_values = {
                name: os.environ.get(name)
                for name in ["ROOT_ONLY", "WORKSPACE_ONLY", "SHARED"]
            }
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
