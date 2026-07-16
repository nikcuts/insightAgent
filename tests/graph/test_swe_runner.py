"""SWE 风格评测与图运行器的直接集成边界。"""

from __future__ import annotations

import json
import asyncio
import os
import signal
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from insightagent.evals.swe_style import (
    CaseRunResult,
    CommandResult,
    SweStyleCase,
    WorkspaceChanges,
    export_predictions,
    render_summary,
    run_command,
    run_case,
    run_process,
    write_reports,
)


def _case(tmp_path: Path) -> SweStyleCase:
    source = tmp_path / "source"
    source.mkdir()
    (source / "calc.py").write_text("VALUE = 0\n", encoding="utf-8")
    (source / "verify.py").write_text(
        "from pathlib import Path\n"
        "raise SystemExit(0 if Path('calc.py').read_text(encoding='utf-8') == 'VALUE = 1\\n' else 1)\n",
        encoding="utf-8",
    )
    return SweStyleCase(
        id="local_graph_eval",
        source_dir=source,
        issue="Repair the existing calculator value.",
        test_command="python verify.py",
    )


def _run(case: SweStyleCase, tmp_path: Path):
    return run_case(
        case,
        run_root=tmp_path / "runs",
        report_root=tmp_path / "reports",
        project_root=tmp_path,
        provider="siliconflow",
        model=None,
        run_id="graph-eval",
        max_wall_seconds=12,
        max_tool_iterations=3,
        max_output_tokens=456,
    )


def _result_with_secrets(
    *,
    high_entropy_secret: str,
    api_secret: str,
    bearer_secret: str,
) -> CaseRunResult:
    return CaseRunResult(
        id=high_entropy_secret,
        status="unresolved",
        resolved=False,
        failure_mode="verification_failed",
        workspace=f"workspaces/API_KEY={api_secret}",
        trace_jsonl=f"reports/Authorization: Bearer {bearer_secret}",
        changes=WorkspaceChanges(
            modified_files=["config.py"],
            added_files=[],
            deleted_files=[],
            test_files_changed=[],
            source_files_changed=["config.py"],
            patch=(
                "--- a/config.py\n"
                "+++ b/config.py\n"
                "@@ -1 +1 @@\n"
                f"+API_KEY={api_secret}\n"
                f"+Authorization: Bearer {bearer_secret}\n"
            ),
        ),
        baseline=CommandResult(
            command=f"runner --api-key {api_secret}",
            exit_code=1,
            stdout=f"API_KEY={api_secret}",
            stderr=f"Authorization: Bearer {bearer_secret}",
            duration_seconds=0.1,
        ),
        agent=CommandResult(
            command=f"runner --api-key {api_secret}",
            exit_code=1,
            stdout=f"API_KEY={api_secret}",
            stderr=f"Authorization: Bearer {bearer_secret}",
            duration_seconds=0.1,
        ),
        verification=CommandResult(
            command=f"runner --api-key {api_secret}",
            exit_code=1,
            stdout=f"API_KEY={api_secret}",
            stderr=f"Authorization: Bearer {bearer_secret}",
            duration_seconds=0.1,
        ),
    )


def test_swe_runner_calls_graph_runner_and_keeps_debug_trace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    case = _case(tmp_path)
    calls: list[dict[str, object]] = []

    def fake_run_task(**kwargs: object) -> SimpleNamespace:
        calls.append(kwargs)
        workspace = kwargs["workspace"]
        assert isinstance(workspace, Path)
        (workspace / "calc.py").write_text("VALUE = 1\n", encoding="utf-8")
        trace_path = kwargs["trace_jsonl"]
        assert isinstance(trace_path, str)
        Path(trace_path).write_text('{"type":"final_state"}\n', encoding="utf-8")
        return SimpleNamespace(
            final_answer="completed",
            state={"phase": "done"},
            trace_id="trace-1",
        )

    monkeypatch.setattr("insightagent.evals.swe_style.graph_run_task", fake_run_task)

    result = _run(case, tmp_path)

    assert calls[0]["task"].startswith("You are solving a SWE-bench-style repository repair task")
    assert calls[0]["tool_profile"] == "coding-basic"
    assert calls[0]["enabled_mcp_servers"] == set()
    assert calls[0]["no_trace"] is True
    config = calls[0]["config"]
    assert getattr(config, "permission_mode") == "workspace-write"
    assert getattr(config, "max_wall_seconds") == 12
    assert result.resolved is True
    assert result.langfuse_trace_id == "trace-1"
    assert result.trace_status == "available"
    assert result.agent is not None and result.agent.command == "graph_run_task"


def test_swe_runner_keeps_external_verification_when_graph_reports_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    case = _case(tmp_path)

    def fake_run_task(**kwargs: object) -> SimpleNamespace:
        trace_path = kwargs["trace_jsonl"]
        assert isinstance(trace_path, str)
        Path(trace_path).write_text('{"type":"final_state"}\n', encoding="utf-8")
        return SimpleNamespace(
            final_answer="unable to repair",
            state={"phase": "failed"},
            trace_id=None,
        )

    monkeypatch.setattr("insightagent.evals.swe_style.graph_run_task", fake_run_task)

    result = _run(case, tmp_path)

    assert result.status == "agent_error"
    assert result.failure_mode == "graph_failed"
    assert result.agent is not None and result.agent.exit_code == 1
    assert result.verification is not None
    assert result.trace_status == "available"


def test_swe_runner_reports_partial_changes_after_graph_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    case = _case(tmp_path)

    def fake_run_task(**kwargs: object) -> SimpleNamespace:
        workspace = kwargs["workspace"]
        assert isinstance(workspace, Path)
        (workspace / "calc.py").write_text("VALUE = 1\n", encoding="utf-8")
        raise TimeoutError("run exceeded budget")

    monkeypatch.setattr("insightagent.evals.swe_style.graph_run_task", fake_run_task)

    result = _run(case, tmp_path)

    assert result.status == "agent_error"
    assert result.resolved is False
    assert result.failure_mode == "graph_timeout"
    assert result.agent is not None and result.agent.timed_out is True
    assert result.verification is not None
    assert result.changes.modified_files == ["calc.py"]
    assert result.trace_status == "unavailable"


def test_swe_runner_marks_graph_time_budget_state_as_timeout_and_reports_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    case = _case(tmp_path)

    def fake_run_task(**kwargs: object) -> SimpleNamespace:
        trace_path = kwargs["trace_jsonl"]
        assert isinstance(trace_path, str)
        Path(trace_path).write_text('{"type":"final_state"}\n', encoding="utf-8")
        return SimpleNamespace(
            final_answer="budget expired",
            state={
                "phase": "failed",
                "tool_events": [{"failure_kind": "time_budget_exceeded"}],
            },
            trace_id=None,
        )

    monkeypatch.setattr("insightagent.evals.swe_style.graph_run_task", fake_run_task)

    result = _run(case, tmp_path)
    jsonl_path, markdown_path = write_reports([result], tmp_path / "reports", "timeout-report")
    payload = json.loads(jsonl_path.read_text(encoding="utf-8"))

    assert result.status == "agent_error"
    assert result.failure_mode == "graph_timeout"
    assert result.agent is not None and result.agent.timed_out is True
    assert payload["failure_mode"] == "graph_timeout"
    assert "graph_timeout" in markdown_path.read_text(encoding="utf-8")


def test_write_reports_redacts_secrets_from_jsonl_and_markdown(tmp_path: Path) -> None:
    high_entropy_secret = "sk-abcdefghijklmnop123456"
    api_secret = "swe-report-secret"
    bearer_secret = "swe-bearer-secret"
    result = _result_with_secrets(
        high_entropy_secret=high_entropy_secret,
        api_secret=api_secret,
        bearer_secret=bearer_secret,
    )

    jsonl_path, markdown_path = write_reports([result], tmp_path / "reports", "secret-report")

    jsonl = jsonl_path.read_text(encoding="utf-8")
    markdown = markdown_path.read_text(encoding="utf-8")
    for secret in (high_entropy_secret, api_secret, bearer_secret):
        assert secret not in jsonl
        assert secret not in markdown
    assert "***REDACTED***" in jsonl
    assert "***REDACTED***" in markdown


def test_render_summary_redacts_secrets_at_its_public_boundary() -> None:
    high_entropy_secret = "sk-abcdefghijklmnop654321"
    api_secret = "swe-summary-secret"
    bearer_secret = "swe-summary-bearer"
    summary = render_summary(
        [
            _result_with_secrets(
                high_entropy_secret=high_entropy_secret,
                api_secret=api_secret,
                bearer_secret=bearer_secret,
            )
        ]
    )

    for secret in (high_entropy_secret, api_secret, bearer_secret):
        assert secret not in summary
    assert "***REDACTED***" in summary


def test_export_predictions_redacts_secrets_from_model_patch(tmp_path: Path) -> None:
    high_entropy_secret = "sk-abcdefghijklmnop654321"
    api_secret = "swe-prediction-secret"
    bearer_secret = "swe-prediction-bearer"
    result = _result_with_secrets(
        high_entropy_secret=high_entropy_secret,
        api_secret=api_secret,
        bearer_secret=bearer_secret,
    )

    predictions_path = export_predictions(
        [result],
        tmp_path / "predictions.jsonl",
        model_name_or_path=high_entropy_secret,
    )

    predictions = predictions_path.read_text(encoding="utf-8")
    for secret in (high_entropy_secret, api_secret, bearer_secret):
        assert secret not in predictions
    assert "***REDACTED***" in predictions


def test_export_predictions_keeps_short_redacted_patch_lines_intact(tmp_path: Path) -> None:
    result = _result_with_secrets(
        high_entropy_secret="sk-abcdefghijklmnop654321",
        api_secret="unused",
        bearer_secret="unused",
    )
    result = replace(result, changes=replace(result.changes, patch="+API_KEY=x\n"))

    predictions_path = export_predictions(
        [result],
        tmp_path / "predictions.jsonl",
        model_name_or_path="model",
    )

    prediction = json.loads(predictions_path.read_text(encoding="utf-8"))

    assert prediction["model_patch"] == "+API_KEY=***REDACTED***\n"


@pytest.mark.skipif(os.name != "posix", reason="process groups require Unix")
def test_run_command_timeout_reclaims_nested_process_tree(tmp_path: Path) -> None:
    pids_path = tmp_path / "run-command-pids"
    command = f"{sys.executable} -c {_child_tree_parent_code(pids_path)!r}"

    try:
        result = run_command(command, cwd=tmp_path, timeout_seconds=0.2)

        assert result.timed_out is True
        parent_pid, child_pid = _wait_for_pids(pids_path)
        assert _process_eventually_gone(parent_pid)
        assert _process_eventually_gone(child_pid)
    finally:
        _kill_recorded_processes(pids_path)


@pytest.mark.skipif(os.name != "posix", reason="process groups require Unix")
def test_run_process_timeout_reclaims_nested_process_tree(tmp_path: Path) -> None:
    pids_path = tmp_path / "run-process-pids"
    command = [sys.executable, "-c", _child_tree_parent_code(pids_path)]

    try:
        result = run_process(command, cwd=tmp_path, timeout_seconds=0.2)

        assert result.timed_out is True
        parent_pid, child_pid = _wait_for_pids(pids_path)
        assert _process_eventually_gone(parent_pid)
        assert _process_eventually_gone(child_pid)
    finally:
        _kill_recorded_processes(pids_path)


@pytest.mark.skipif(os.name != "posix", reason="process groups require Unix")
def test_run_process_timeout_keeps_only_output_captured_before_the_deadline(tmp_path: Path) -> None:
    command = [
        sys.executable,
        "-c",
        (
            "import signal, time; "
            "signal.signal(signal.SIGTERM, lambda *_: print('after', flush=True)); "
            "print('before', flush=True); time.sleep(30)"
        ),
    ]

    result = run_process(command, cwd=tmp_path, timeout_seconds=0.2)

    assert result.timed_out is True
    assert result.stdout == "before\n"


def test_swe_runner_reports_graph_exception_after_partial_workspace_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    case = _case(tmp_path)

    def fake_run_task(**kwargs: object) -> SimpleNamespace:
        workspace = kwargs["workspace"]
        assert isinstance(workspace, Path)
        (workspace / "calc.py").write_text("VALUE = 2\n", encoding="utf-8")
        trace_path = kwargs["trace_jsonl"]
        assert isinstance(trace_path, str)
        Path(trace_path).write_text(
            '{"type":"graph_turn","trace_id":"trace-cleanup"}\n', encoding="utf-8"
        )
        raise RuntimeError("unexpected graph cleanup failure")

    monkeypatch.setattr("insightagent.evals.swe_style.graph_run_task", fake_run_task)

    result = _run(case, tmp_path)
    jsonl_path, markdown_path = write_reports([result], tmp_path / "reports", "error-report")
    payload = json.loads(jsonl_path.read_text(encoding="utf-8"))

    assert result.status == "agent_error"
    assert result.failure_mode == "graph_error"
    assert result.verification is not None
    assert result.changes.modified_files == ["calc.py"]
    assert result.langfuse_trace_id == "trace-cleanup"
    assert payload["trace_status"] == "available"
    assert "graph_error" in markdown_path.read_text(encoding="utf-8")


def test_swe_runner_reports_cancelled_graph_after_partial_workspace_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    case = _case(tmp_path)

    def fake_run_task(**kwargs: object) -> SimpleNamespace:
        workspace = kwargs["workspace"]
        assert isinstance(workspace, Path)
        (workspace / "calc.py").write_text("VALUE = 2\n", encoding="utf-8")
        raise asyncio.CancelledError()

    monkeypatch.setattr("insightagent.evals.swe_style.graph_run_task", fake_run_task)

    result = _run(case, tmp_path)

    assert result.status == "agent_error"
    assert result.failure_mode == "graph_cancelled"
    assert result.verification is not None
    assert result.changes.modified_files == ["calc.py"]


def test_subprocess_runner_extracts_trace_id_from_debug_jsonl(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    case = _case(tmp_path)

    def fake_run_process(
        command: list[str],
        cwd: Path,
        timeout_seconds: float,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        del cwd, timeout_seconds, env
        workspace = Path(command[command.index("--workspace") + 1])
        trace_path = Path(command[command.index("--trace-jsonl") + 1])
        (workspace / "calc.py").write_text("VALUE = 1\n", encoding="utf-8")
        trace_path.write_text('{"type":"graph_turn","trace_id":"trace-child"}\n', encoding="utf-8")
        return CommandResult("graph-cli", 0, "completed", "", 0.0)

    monkeypatch.setattr("insightagent.evals.swe_style.run_process", fake_run_process)

    result = run_case(
        case,
        run_root=tmp_path / "runs",
        report_root=tmp_path / "reports",
        project_root=tmp_path,
        provider="siliconflow",
        model=None,
        run_id="subprocess-eval",
        subprocess_mode=True,
    )

    assert result.resolved is True
    assert result.trace_status == "available"
    assert result.langfuse_trace_id == "trace-child"


def test_subprocess_runner_maps_graph_time_budget_event_to_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    case = _case(tmp_path)

    def fake_run_process(
        command: list[str],
        cwd: Path,
        timeout_seconds: float,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        del cwd, timeout_seconds, env
        trace_path = Path(command[command.index("--trace-jsonl") + 1])
        trace_path.write_text(
            '{"type":"graph_turn","tool_events":[{"failure_kind":"time_budget_exceeded"}]}\n',
            encoding="utf-8",
        )
        return CommandResult("graph-cli", 1, "", "failed", 0.0)

    monkeypatch.setattr("insightagent.evals.swe_style.run_process", fake_run_process)

    result = run_case(
        case,
        run_root=tmp_path / "runs",
        report_root=tmp_path / "reports",
        project_root=tmp_path,
        provider="siliconflow",
        model=None,
        run_id="subprocess-timeout",
        subprocess_mode=True,
    )

    assert result.status == "agent_error"
    assert result.failure_mode == "graph_timeout"
    assert result.agent is not None and result.agent.timed_out is True


def test_subprocess_runner_does_not_add_an_outer_timeout_for_unlimited_graph_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    case = _case(tmp_path)
    received_timeout: float | None = 1.0

    def fake_run_process(
        command: list[str],
        cwd: Path,
        timeout_seconds: float | None,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        nonlocal received_timeout
        del command, cwd, env
        received_timeout = timeout_seconds
        return CommandResult("graph-cli", 1, "", "failed", 0.0)

    monkeypatch.setattr("insightagent.evals.swe_style.run_process", fake_run_process)

    run_case(
        case,
        run_root=tmp_path / "runs",
        report_root=tmp_path / "reports",
        project_root=tmp_path,
        provider="siliconflow",
        model=None,
        run_id="subprocess-unlimited",
        max_wall_seconds=0,
        subprocess_mode=True,
    )

    assert received_timeout is None


def test_swe_runner_sanitizes_case_id_when_creating_trace_path(tmp_path: Path) -> None:
    source_case = _case(tmp_path)
    case = SweStyleCase(
        id="../../outside-trace",
        source_dir=source_case.source_dir,
        issue=source_case.issue,
        test_command=source_case.test_command,
    )
    report_root = tmp_path / "reports"

    result = run_case(
        case,
        run_root=tmp_path / "runs",
        report_root=report_root,
        project_root=tmp_path,
        provider="siliconflow",
        model=None,
        run_id="safe-run",
        dry_run=True,
    )

    trace_path = Path(result.trace_jsonl).resolve()
    assert trace_path.parent == (report_root / "safe-run").resolve()
    assert trace_path.name == "outside-trace.trace.jsonl"


def test_swe_runner_marks_empty_or_invalid_debug_trace_as_unavailable(tmp_path: Path) -> None:
    from insightagent.evals.swe_style import _trace_status

    empty = tmp_path / "empty.trace.jsonl"
    invalid = tmp_path / "invalid.trace.jsonl"
    empty.write_text("", encoding="utf-8")
    invalid.write_text("not-json\n", encoding="utf-8")

    assert _trace_status(empty) == "unavailable"
    assert _trace_status(invalid) == "unavailable"


def _child_tree_parent_code(pids_path: Path) -> str:
    child_code = (
        "import signal, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "time.sleep(30)"
    )
    return (
        "import os, pathlib, subprocess, sys, time; "
        f"child = subprocess.Popen([sys.executable, '-c', {child_code!r}], "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
        f"pathlib.Path({str(pids_path)!r}).write_text(str(os.getpid()) + ' ' + str(child.pid)); "
        "os.close(sys.stdout.fileno()); os.close(sys.stderr.fileno()); "
        "time.sleep(30)"
    )


def _wait_for_pids(pids_path: Path) -> tuple[int, int]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if pids_path.is_file():
            parent_pid, child_pid = pids_path.read_text(encoding="utf-8").split()
            return int(parent_pid), int(child_pid)
        time.sleep(0.02)
    raise AssertionError(f"process tree did not write pids to {pids_path}")


def _process_eventually_gone(pid: int) -> bool:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.02)
    return False


def _kill_recorded_processes(pids_path: Path) -> None:
    if not pids_path.is_file():
        return
    for pid in _wait_for_pids(pids_path):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
