from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from insightagent.evals.swe_style import (
    CaseRunResult,
    CommandResult,
    SweStyleCase,
    WorkspaceChanges,
    analyze_existing_run,
    analyze_workspace_changes,
    build_agent_command,
    build_task_prompt,
    export_predictions,
    load_cases,
    prepare_workspace,
    render_summary,
    run_case,
    run_command,
)


ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "tests" / "fixtures" / "swe_style" / "cases.jsonl"


class SweStyleEvalTests(unittest.TestCase):
    def test_load_cases_resolves_relative_source_dirs(self) -> None:
        case = load_cases(DATASET)[0]

        self.assertEqual(case.id, "local_calc_addition")
        self.assertTrue(case.source_dir.is_dir())
        self.assertIn("addition", case.issue)

    def test_load_cases_accepts_swe_bench_style_jsonl_with_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "django__django-12345"
            checkout.mkdir()
            dataset = root / "swebench.jsonl"
            dataset.write_text(
                '{"instance_id":"django__django-12345",'
                '"repo":"django/django",'
                '"base_commit":"abc123",'
                '"problem_statement":"Fix a queryset regression.",'
                '"source_dir":"django__django-12345",'
                '"test_command":"python -m pytest tests/test_qs.py",'
                '"FAIL_TO_PASS":["tests/test_qs.py::test_regression"]}\n',
                encoding="utf-8",
            )

            case = load_cases(dataset)[0]

        self.assertEqual(case.id, "django__django-12345")
        self.assertEqual(case.issue, "Fix a queryset regression.")
        self.assertEqual(case.source_dir, checkout.resolve())
        self.assertEqual(case.test_command, "python -m pytest tests/test_qs.py")
        self.assertEqual(case.metadata["repo"], "django/django")
        self.assertEqual(case.metadata["base_commit"], "abc123")
        self.assertEqual(case.metadata["FAIL_TO_PASS"], ["tests/test_qs.py::test_regression"])

    def test_load_cases_can_derive_swe_bench_source_dir_from_checkout_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout_root = root / "checkouts"
            checkout = checkout_root / "sympy__sympy-24680"
            checkout.mkdir(parents=True)
            dataset = root / "swebench.jsonl"
            dataset.write_text(
                '{"instance_id":"sympy__sympy-24680",'
                '"problem_statement":"Fix simplify regression.",'
                '"verification_command":"python -m pytest sympy/test_simplify.py"}\n',
                encoding="utf-8",
            )

            case = load_cases(dataset, checkout_root=checkout_root)[0]

        self.assertEqual(case.id, "sympy__sympy-24680")
        self.assertEqual(case.source_dir, checkout.resolve())
        self.assertEqual(case.test_command, "python -m pytest sympy/test_simplify.py")

    def test_fixture_baseline_fails_before_agent_repair(self) -> None:
        case = load_cases(DATASET)[0]
        with tempfile.TemporaryDirectory() as directory:
            workspace = prepare_workspace(case, Path(directory) / "runs", "unit")
            result = run_command(case.test_command, cwd=workspace, timeout_seconds=10)

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("FAILED", result.stderr)

    def test_dry_run_records_baseline_without_calling_provider(self) -> None:
        case = load_cases(DATASET)[0]
        with tempfile.TemporaryDirectory() as directory:
            result = run_case(
                case,
                run_root=Path(directory) / "runs",
                report_root=Path(directory) / "reports",
                project_root=ROOT,
                provider="siliconflow",
                model="Qwen/Qwen2.5-72B-Instruct",
                run_id="unit",
                dry_run=True,
            )

        self.assertEqual(result.status, "dry_run")
        self.assertFalse(result.resolved)
        self.assertIsNone(result.agent)
        self.assertIsNone(result.verification)
        self.assertNotEqual(result.baseline.exit_code, 0)

    def test_agent_command_keeps_multiline_task_as_one_argument(self) -> None:
        case = load_cases(DATASET)[0]
        task = build_task_prompt(case)
        command = build_agent_command(
            workspace=Path("workspace"),
            trace_path=Path("trace.jsonl"),
            task=task,
            provider="siliconflow",
            model="Qwen/Qwen2.5-72B-Instruct",
            provider_timeout=300,
            max_wall_seconds=120,
            max_tool_iterations=12,
            max_output_tokens=4096,
            language="Chinese",
        )

        task_index = command.index("--task")
        self.assertEqual(command[task_index + 1], task)
        self.assertIn("--no-trace", command)
        self.assertIn("first tool call must inspect", task)
        self.assertIn(case.test_command, task)

    def test_analyzes_workspace_changes_for_swe_style_failure_modes(self) -> None:
        case = load_cases(DATASET)[0]
        with tempfile.TemporaryDirectory() as directory:
            workspace = prepare_workspace(case, Path(directory) / "runs", "unit")
            (workspace / "calc.py").write_text(
                "def calculate(left: int, operator: str, right: int) -> int:\n"
                "    return left + right\n",
                encoding="utf-8",
            )
            (workspace / "test_calc.py").write_text("# changed test\n", encoding="utf-8")
            (workspace / "addition.py").write_text("print('demo')\n", encoding="utf-8")

            changes = analyze_workspace_changes(case.source_dir, workspace)

        self.assertEqual(changes.modified_files, ["calc.py", "test_calc.py"])
        self.assertEqual(changes.added_files, ["addition.py"])
        self.assertEqual(changes.deleted_files, [])
        self.assertEqual(changes.test_files_changed, ["test_calc.py"])
        self.assertEqual(changes.source_files_changed, ["addition.py", "calc.py"])
        self.assertIn("--- a/calc.py", changes.patch)
        self.assertIn("+++ b/calc.py", changes.patch)
        self.assertIn("+    return left + right", changes.patch)
        self.assertIn("--- /dev/null", changes.patch)
        self.assertIn("+++ b/addition.py", changes.patch)

    def test_test_modification_is_not_counted_as_resolved(self) -> None:
        case = load_cases(DATASET)[0]

        def fake_run_process(command, cwd, timeout_seconds, env=None):  # noqa: ANN001
            workspace = Path(command[command.index("--workspace") + 1])
            (workspace / "test_calc.py").write_text(
                "import unittest\n\n"
                "class CalculatorTests(unittest.TestCase):\n"
                "    def test_bypassed(self) -> None:\n"
                "        self.assertTrue(True)\n",
                encoding="utf-8",
            )
            return CommandResult(
                command="fake-agent",
                exit_code=0,
                stdout="changed tests",
                stderr="",
                duration_seconds=0.0,
            )

        with tempfile.TemporaryDirectory() as directory:
            with patch("insightagent.evals.swe_style.run_process", fake_run_process):
                result = run_case(
                    case,
                    run_root=Path(directory) / "runs",
                    report_root=Path(directory) / "reports",
                    project_root=ROOT,
                    provider="siliconflow",
                    model="Qwen/Qwen2.5-72B-Instruct",
                    run_id="unit",
                )

        self.assertEqual(result.status, "invalid_test_modified")
        self.assertFalse(result.resolved)
        self.assertEqual(result.verification.exit_code, 0)
        self.assertEqual(result.changes.test_files_changed, ["test_calc.py"])
        self.assertEqual(result.failure_mode, "test_modified")

    def test_only_added_demo_file_is_classified_as_no_existing_source_patch(self) -> None:
        case = load_cases(DATASET)[0]

        def fake_run_process(command, cwd, timeout_seconds, env=None):  # noqa: ANN001
            workspace = Path(command[command.index("--workspace") + 1])
            (workspace / "addition.py").write_text("print('demo')\n", encoding="utf-8")
            return CommandResult(
                command="fake-agent",
                exit_code=0,
                stdout="created demo",
                stderr="",
                duration_seconds=0.0,
            )

        with tempfile.TemporaryDirectory() as directory:
            with patch("insightagent.evals.swe_style.run_process", fake_run_process):
                result = run_case(
                    case,
                    run_root=Path(directory) / "runs",
                    report_root=Path(directory) / "reports",
                    project_root=ROOT,
                    provider="siliconflow",
                    model="Qwen/Qwen2.5-72B-Instruct",
                    run_id="unit",
                )

        self.assertEqual(result.status, "unresolved")
        self.assertFalse(result.resolved)
        self.assertEqual(result.changes.added_files, ["addition.py"])
        self.assertEqual(result.changes.modified_files, [])
        self.assertEqual(result.failure_mode, "only_added_files")

    def test_summary_separates_evaluable_cases_from_invalid_baselines(self) -> None:
        results = [
            _case_result("pallets__flask-4045", "resolved", True, baseline=1, agent=0, verification=0),
            _case_result("pallets__flask-5063", "resolved", True, baseline=1, agent=0, verification=0),
            _case_result("pallets__flask-4992", "invalid_baseline", False, baseline=0),
            _case_result("psf__requests-3362", "invalid_environment", False, baseline=4),
        ]

        summary = render_summary(results)

        self.assertIn("- Total cases: 4", summary)
        self.assertIn("- Evaluable cases: 2", summary)
        self.assertIn("- Invalid baseline cases: 1", summary)
        self.assertIn("- Invalid environment cases: 1", summary)
        self.assertIn("- Resolved: 2", summary)
        self.assertIn("- Resolution rate: 100.0%", summary)
        self.assertIn("- Raw resolution rate: 50.0%", summary)

    def test_summary_includes_trace_path_and_patch_size(self) -> None:
        result = _case_result(
            "pallets__flask-5063",
            "unresolved",
            False,
            baseline=1,
            agent=0,
            verification=1,
            patch=(
                "--- a/src/flask/cli.py\n"
                "+++ b/src/flask/cli.py\n"
                "@@ -1,2 +1,3 @@\n"
                "-old\n"
                "+new\n"
                "+added\n"
            ),
        )

        summary = render_summary([result])

        self.assertIn("Trace", summary)
        self.assertIn("Patch +", summary)
        self.assertIn("Patch -", summary)
        self.assertIn("`reports/swe_style/pallets__flask-5063.trace.jsonl`", summary)
        self.assertIn("| 2 | 1 |", summary)

    def test_invalid_pytest_collection_baseline_does_not_call_agent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "fail_env.py").write_text(
                "import sys\n"
                "sys.stderr.write(\"ImportError while loading conftest 'tests/conftest.py'.\\n\")\n"
                "sys.stderr.write(\"E   AttributeError: module 'collections' has no attribute 'MutableMapping'\\n\")\n"
                "raise SystemExit(4)\n",
                encoding="utf-8",
            )
            case = SweStyleCase(
                id="psf__requests-3362",
                source_dir=source,
                issue="Uncertain about content/text vs iter_content(decode_unicode=True/False)",
                test_command="python fail_env.py",
                metadata={"repo": "psf/requests"},
            )

            result = run_case(
                case,
                run_root=root / "runs",
                report_root=root / "reports",
                project_root=ROOT,
                provider="provider-should-not-be-called",
                model="Qwen/Qwen2.5-72B-Instruct",
                run_id="unit-invalid-env",
                max_wall_seconds=1,
            )

        self.assertEqual(result.status, "invalid_environment")
        self.assertEqual(result.failure_mode, "invalid_environment")
        self.assertEqual(result.baseline.exit_code, 4)
        self.assertIsNone(result.agent)
        self.assertIsNone(result.verification)

    def test_dry_run_classifies_import_error_baseline_as_invalid_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "fail_env.py").write_text(
                "import sys\n"
                "sys.stderr.write(\"ModuleNotFoundError: No module named '_pytest._version'\\n\")\n"
                "raise SystemExit(1)\n",
                encoding="utf-8",
            )
            case = SweStyleCase(
                id="pytest-dev__pytest-11143",
                source_dir=source,
                issue="Rewrite fails when first expression of file is a number.",
                test_command="python fail_env.py",
                metadata={"repo": "pytest-dev/pytest"},
            )

            result = run_case(
                case,
                run_root=root / "runs",
                report_root=root / "reports",
                project_root=ROOT,
                provider="provider-should-not-be-called",
                model="Qwen/Qwen2.5-72B-Instruct",
                run_id="unit-dry-invalid-env",
                dry_run=True,
                max_wall_seconds=1,
            )

        self.assertEqual(result.status, "invalid_environment")
        self.assertEqual(result.failure_mode, "invalid_environment")
        self.assertEqual(result.baseline.exit_code, 1)
        self.assertIsNone(result.agent)
        self.assertIsNone(result.verification)

    def test_analyze_existing_run_rebuilds_report_without_calling_provider(self) -> None:
        case = load_cases(DATASET)[0]
        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "runs"
            workspace = prepare_workspace(case, run_root, "existing")
            (workspace / "addition.py").write_text("print('demo')\n", encoding="utf-8")

            result = analyze_existing_run(
                case,
                run_root=run_root,
                report_root=Path(directory) / "reports",
                run_id="existing",
                test_timeout=10,
            )

        self.assertEqual(result.status, "unresolved")
        self.assertEqual(result.failure_mode, "only_added_files")
        self.assertIsNone(result.agent)
        self.assertNotEqual(result.baseline.exit_code, 0)
        self.assertNotEqual(result.verification.exit_code, 0)

    def test_exports_swe_bench_predictions_jsonl(self) -> None:
        case = load_cases(DATASET)[0]

        def fake_run_process(command, cwd, timeout_seconds, env=None):  # noqa: ANN001
            workspace = Path(command[command.index("--workspace") + 1])
            (workspace / "calc.py").write_text(
                "def calculate(left: int, operator: str, right: int) -> int:\n"
                "    if operator == '+':\n"
                "        return left + right\n"
                "    if operator == '-':\n"
                "        return left - right\n"
                "    if operator == '*':\n"
                "        return left * right\n"
                "    raise ValueError(f\"unsupported operator: {operator}\")\n",
                encoding="utf-8",
            )
            return CommandResult(
                command="fake-agent",
                exit_code=0,
                stdout="patched calc.py",
                stderr="",
                duration_seconds=0.0,
            )

        with tempfile.TemporaryDirectory() as directory:
            with patch("insightagent.evals.swe_style.run_process", fake_run_process):
                result = run_case(
                    case,
                    run_root=Path(directory) / "runs",
                    report_root=Path(directory) / "reports",
                    project_root=ROOT,
                    provider="siliconflow",
                    model="Qwen/Qwen2.5-72B-Instruct",
                    run_id="unit",
                )
            predictions = export_predictions(
                [result],
                Path(directory) / "predictions.jsonl",
                model_name_or_path="Qwen/Qwen2.5-72B-Instruct",
            )
            prediction = json.loads(predictions.read_text(encoding="utf-8"))

        self.assertTrue(result.resolved)
        self.assertEqual(prediction["instance_id"], "local_calc_addition")
        self.assertEqual(prediction["model_name_or_path"], "Qwen/Qwen2.5-72B-Instruct")
        self.assertIn("--- a/calc.py", prediction["model_patch"])
        self.assertIn("+        return left + right", prediction["model_patch"])
        self.assertEqual(set(prediction), {"instance_id", "model_name_or_path", "model_patch"})


def _case_result(
    case_id: str,
    status: str,
    resolved: bool,
    *,
    baseline: int,
    agent: int | None = None,
    verification: int | None = None,
    patch: str = "",
) -> CaseRunResult:
    changes = WorkspaceChanges(
        modified_files=[],
        added_files=[],
        deleted_files=[],
        test_files_changed=[],
        source_files_changed=[],
        patch=patch,
    )
    return CaseRunResult(
        id=case_id,
        status=status,
        resolved=resolved,
        failure_mode=status,
        workspace=f"workspaces/evals/swe_style/{case_id}",
        trace_jsonl=f"reports/swe_style/{case_id}.trace.jsonl",
        changes=changes,
        baseline=CommandResult("python -m pytest -q", baseline, "", "", 0.0),
        agent=None if agent is None else CommandResult("agent", agent, "", "", 0.0),
        verification=None if verification is None else CommandResult("python -m pytest -q", verification, "", "", 0.0),
    )


if __name__ == "__main__":
    unittest.main()
