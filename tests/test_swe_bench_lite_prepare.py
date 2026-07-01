from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from insightagent.evals.swe_bench_lite import (
    build_verification_command,
    case_record_from_row,
    prepare_checkout,
    select_rows,
    write_cases_jsonl,
)


class SweBenchLitePrepareTests(unittest.TestCase):
    def test_case_record_uses_fail_to_pass_as_verification_command_without_gold_patch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            row = {
                "instance_id": "psf__requests-3362",
                "repo": "psf/requests",
                "base_commit": "abc123",
                "problem_statement": "Fix unicode decoding.",
                "FAIL_TO_PASS": json.dumps(["tests/test_requests.py::TestRequests::test_response_decode_unicode"]),
                "PASS_TO_PASS": json.dumps(["tests/test_requests.py::TestRequests::test_response_iter_lines"]),
                "environment_setup_commit": "setup123",
                "patch": "gold source patch",
                "test_patch": "gold test patch",
            }

            record = case_record_from_row(row, checkout_root=Path(directory), test_patch_applied=True)

        self.assertEqual(record["instance_id"], "psf__requests-3362")
        self.assertEqual(record["verification_command"], "python -m pytest -q tests/test_requests.py::TestRequests::test_response_decode_unicode")
        self.assertTrue(record["source_dir"].endswith("psf__requests-3362"))
        self.assertTrue(record["test_patch_applied"])
        self.assertNotIn("patch", record)
        self.assertNotIn("test_patch", record)

    def test_select_rows_filters_by_repo_instance_ids_and_limit(self) -> None:
        rows = [
            {"instance_id": "django__django-1", "repo": "django/django"},
            {"instance_id": "psf__requests-1", "repo": "psf/requests"},
            {"instance_id": "psf__requests-2", "repo": "psf/requests"},
        ]

        selected = select_rows(rows, repo="psf/requests", instance_ids={"psf__requests-1", "psf__requests-2"}, limit=1)

        self.assertEqual([row["instance_id"] for row in selected], ["psf__requests-1"])

    def test_write_cases_jsonl_outputs_one_json_object_per_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = write_cases_jsonl(
                [{"instance_id": "a", "problem_statement": "one"}, {"instance_id": "b", "problem_statement": "two"}],
                Path(directory) / "nested" / "cases.jsonl",
            )

            lines = output.read_text(encoding="utf-8").splitlines()

        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["instance_id"], "a")
        self.assertEqual(json.loads(lines[1])["instance_id"], "b")

    def test_prepare_checkout_resets_to_base_commit_and_applies_test_patch(self) -> None:
        commands: list[tuple[list[str], str | None]] = []
        row = {
            "instance_id": "psf__requests-3362",
            "repo": "psf/requests",
            "base_commit": "abc123",
            "test_patch": "diff --git a/tests/test_requests.py b/tests/test_requests.py\n",
        }

        with tempfile.TemporaryDirectory() as directory:
            checkout_root = Path(directory) / "checkouts"

            def runner(command: list[str], stdin: str | None) -> None:
                commands.append((command, stdin))
                if command[:2] == ["git", "clone"]:
                    Path(command[-1]).mkdir(parents=True)

            destination = prepare_checkout(row, checkout_root, command_runner=runner)

        self.assertTrue(str(destination).endswith("psf__requests-3362"))
        self.assertEqual(commands[0][0][:2], ["git", "clone"])
        self.assertIn("--filter=blob:none", commands[0][0])
        self.assertIn("--no-checkout", commands[0][0])
        self.assertEqual(commands[1][0][-2:], ["--force", "abc123"])
        self.assertEqual(commands[2][0][-2:], ["clean", "-fdx"])
        self.assertEqual(commands[3][0][-3:], ["apply", "--whitespace=nowarn", "-"])
        self.assertEqual(commands[3][1], row["test_patch"])

    def test_prepare_checkout_can_disable_git_proxy_without_changing_global_config(self) -> None:
        commands: list[tuple[list[str], str | None]] = []
        row = {
            "instance_id": "psf__requests-3362",
            "repo": "psf/requests",
            "base_commit": "abc123",
            "test_patch": "",
        }

        with tempfile.TemporaryDirectory() as directory:
            def runner(command: list[str], stdin: str | None) -> None:
                commands.append((command, stdin))
                if command[-2:] == ["psf/requests.git", str(Path(command[-1]))]:
                    Path(command[-1]).mkdir(parents=True)

            prepare_checkout(row, Path(directory), command_runner=runner, disable_git_proxy=True)

        for command, _stdin in commands:
            self.assertEqual(command[:5], ["git", "-c", "http.proxy=", "-c", "https.proxy="])

    def test_prepare_checkout_can_clone_from_existing_local_repository(self) -> None:
        commands: list[tuple[list[str], str | None]] = []
        row = {
            "instance_id": "pallets__flask-4045",
            "repo": "pallets/flask",
            "base_commit": "def456",
            "test_patch": "",
        }

        with tempfile.TemporaryDirectory() as directory:
            local_source = Path(directory) / "pallets__flask-4992"
            local_source.mkdir()

            def runner(command: list[str], stdin: str | None) -> None:
                commands.append((command, stdin))
                if command[:2] == ["git", "clone"]:
                    Path(command[-1]).mkdir(parents=True)

            prepare_checkout(
                row,
                Path(directory) / "checkouts",
                command_runner=runner,
                clone_from_local=local_source,
            )

        self.assertEqual(commands[0][0], ["git", "clone", str(local_source), commands[0][0][-1]])

    def test_build_verification_command_handles_empty_fail_to_pass(self) -> None:
        self.assertEqual(build_verification_command({"FAIL_TO_PASS": "[]"}), "python -m pytest -q")


if __name__ == "__main__":
    unittest.main()
