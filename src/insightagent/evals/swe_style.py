"""Run local SWE-bench-style repair evaluations.

The harness copies each broken repository fixture into an isolated workspace,
records the baseline verification result, invokes the LangGraph runner in the
same process by default, then runs the same verification command again. A case
is resolved only when the baseline fails and the final verification passes.
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import RuntimeConfig, load_dotenv_files
from ..graph.models import ModelConfigurationError
from ..graph.observability import sanitize_for_model_trace_and_persistence
from ..graph.runner import run_task as graph_run_task
from ..mcp.config import MCPConfig
from ..mcp.errors import MCPConfigError


_PROCESS_TERMINATION_GRACE_SECONDS = 0.2


@dataclass(frozen=True)
class SweStyleCase:
    id: str
    source_dir: Path
    issue: str
    test_command: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CommandResult:
    command: str
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False


@dataclass(frozen=True)
class WorkspaceChanges:
    modified_files: list[str]
    added_files: list[str]
    deleted_files: list[str]
    test_files_changed: list[str]
    source_files_changed: list[str]
    patch: str

    @property
    def changed_files(self) -> list[str]:
        return sorted({*self.modified_files, *self.added_files, *self.deleted_files})


@dataclass(frozen=True)
class CaseRunResult:
    id: str
    status: str
    resolved: bool
    failure_mode: str
    workspace: str
    trace_jsonl: str
    changes: WorkspaceChanges
    baseline: CommandResult
    agent: CommandResult | None
    verification: CommandResult | None
    langfuse_trace_id: str | None = None
    trace_status: str = "unavailable"


def load_cases(dataset_path: str | Path, checkout_root: str | Path | None = None) -> list[SweStyleCase]:
    path = Path(dataset_path).expanduser().resolve()
    base_dir = path.parent
    checkout_root_path = Path(checkout_root).expanduser().resolve() if checkout_root else None
    cases: list[SweStyleCase] = []
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        data = json.loads(line)
        case_id = data.get("id") or data.get("instance_id")
        issue = data.get("issue") or data.get("problem_statement")
        test_command = data.get("test_command") or data.get("verification_command")
        missing = [
            name
            for name, value in {
                "id/instance_id": case_id,
                "issue/problem_statement": issue,
                "test_command/verification_command": test_command,
            }.items()
            if value is None
        ]
        if "source_dir" not in data and checkout_root_path is None:
            missing.append("source_dir or --checkout-root")
        if missing:
            raise ValueError(f"{path}:{line_number} missing fields: {', '.join(missing)}")
        if "source_dir" in data:
            source_dir = Path(str(data["source_dir"]))
        else:
            source_dir = checkout_root_path / _safe_name(str(case_id))
        if not source_dir.is_absolute():
            source_dir = (base_dir / source_dir).resolve()
        if not source_dir.is_dir():
            raise ValueError(f"{path}:{line_number} source_dir is not a directory: {source_dir}")
        cases.append(
            SweStyleCase(
                id=str(case_id),
                source_dir=source_dir,
                issue=str(issue),
                test_command=str(test_command),
                metadata=_case_metadata(data),
            )
        )
    if not cases:
        raise ValueError(f"no cases found in {path}")
    return cases


def run_command(
    command: str,
    cwd: str | Path,
    timeout_seconds: float,
    env: dict[str, str] | None = None,
) -> CommandResult:
    return _run_subprocess(
        command,
        cwd=cwd,
        timeout_seconds=timeout_seconds,
        env=env,
        shell=True,
        display_command=command,
    )


def run_case(
    case: SweStyleCase,
    *,
    run_root: str | Path,
    report_root: str | Path,
    project_root: str | Path,
    provider: str,
    model: str | None,
    run_id: str,
    dry_run: bool = False,
    test_timeout: float = 60.0,
    provider_timeout: int = 300,
    max_wall_seconds: float = 300.0,
    max_tool_iterations: int = 12,
    max_output_tokens: int = 4096,
    language: str = "Chinese",
    subprocess_mode: bool = False,
) -> CaseRunResult:
    workspace = prepare_workspace(case, run_root, run_id)
    trace_path = _trace_path(report_root, run_id, case.id)
    trace_path.parent.mkdir(parents=True, exist_ok=True)

    baseline = run_command(case.test_command, cwd=workspace, timeout_seconds=test_timeout)
    invalid_baseline_status = classify_invalid_baseline(baseline)
    if invalid_baseline_status is not None:
        changes = analyze_workspace_changes(case.source_dir, workspace)
        return CaseRunResult(
            id=case.id,
            status=invalid_baseline_status,
            resolved=False,
            failure_mode=invalid_baseline_status,
            workspace=str(workspace),
            trace_jsonl=str(trace_path),
            changes=changes,
            baseline=baseline,
            agent=None,
            verification=None,
            trace_status=_trace_status(trace_path),
        )

    if dry_run:
        changes = analyze_workspace_changes(case.source_dir, workspace)
        return CaseRunResult(
            id=case.id,
            status="dry_run",
            resolved=False,
            failure_mode="not_run",
            workspace=str(workspace),
            trace_jsonl=str(trace_path),
            changes=changes,
            baseline=baseline,
            agent=None,
            verification=None,
            trace_status=_trace_status(trace_path),
        )

    task = build_task_prompt(case)
    if subprocess_mode:
        agent = run_process(
            build_subprocess_command(
                workspace=workspace,
                trace_path=trace_path,
                task=task,
                provider=provider,
                model=model,
                provider_timeout=provider_timeout,
                max_wall_seconds=max_wall_seconds,
                max_tool_iterations=max_tool_iterations,
                max_output_tokens=max_output_tokens,
                language=language,
            ),
            cwd=project_root,
            timeout_seconds=max_wall_seconds + 30 if max_wall_seconds > 0 else None,
            env=_subprocess_env(project_root),
        )
        langfuse_trace_id = _trace_id_from_debug_trace(trace_path)
        if agent.timed_out:
            agent_failure_mode = "subprocess_timeout"
        elif _debug_trace_has_failure_kind(trace_path, "time_budget_exceeded"):
            agent = CommandResult(
                command=agent.command,
                exit_code=124,
                stdout=agent.stdout,
                stderr=agent.stderr,
                duration_seconds=agent.duration_seconds,
                timed_out=True,
            )
            agent_failure_mode = "graph_timeout"
        else:
            agent_failure_mode = "subprocess_error" if agent.exit_code != 0 else None
    else:
        agent, langfuse_trace_id, agent_failure_mode = _run_graph_case(
            task=task,
            workspace=workspace,
            trace_path=trace_path,
            provider=provider,
            model=model,
            provider_timeout=provider_timeout,
            max_wall_seconds=max_wall_seconds,
            max_tool_iterations=max_tool_iterations,
            max_output_tokens=max_output_tokens,
            language=language,
            session_id=f"swe-{_safe_name(run_id)}-{_safe_name(case.id)}",
        )
    verification = run_command(case.test_command, cwd=workspace, timeout_seconds=test_timeout)
    changes = analyze_workspace_changes(case.source_dir, workspace)
    verification_resolved = (
        baseline.exit_code != 0 and verification.exit_code == 0 and not changes.test_files_changed
    )
    resolved = verification_resolved and agent.exit_code == 0 and agent_failure_mode is None
    if baseline.exit_code == 0:
        status = "invalid_baseline"
    elif agent_failure_mode is not None or agent.exit_code != 0:
        status = "agent_error"
    elif resolved:
        status = "resolved"
    elif verification.exit_code == 0 and changes.test_files_changed:
        status = "invalid_test_modified"
    elif agent.exit_code != 0:
        status = "agent_error"
    else:
        status = "unresolved"
    failure_mode = classify_failure_mode(
        baseline=baseline,
        agent=agent,
        verification=verification,
        changes=changes,
        status=status,
        resolved=resolved,
        agent_failure_mode=agent_failure_mode,
    )
    return CaseRunResult(
        id=case.id,
        status=status,
        resolved=resolved,
        failure_mode=failure_mode,
        workspace=str(workspace),
        trace_jsonl=str(trace_path),
        changes=changes,
        baseline=baseline,
        agent=agent,
        verification=verification,
        langfuse_trace_id=langfuse_trace_id,
        trace_status=_trace_status(trace_path),
    )


def analyze_existing_run(
    case: SweStyleCase,
    *,
    run_root: str | Path,
    report_root: str | Path,
    run_id: str,
    test_timeout: float = 60.0,
) -> CaseRunResult:
    run_root_path = Path(run_root).expanduser().resolve()
    workspace = run_root_path / run_id / _safe_name(case.id)
    if not workspace.is_dir():
        raise FileNotFoundError(f"existing run workspace not found: {workspace}")
    trace_path = _trace_path(report_root, run_id, case.id)
    baseline_workspace = prepare_workspace(case, run_root_path, f"{run_id}.__baseline__.{_safe_name(case.id)}")
    baseline_parent = baseline_workspace.parent
    try:
        baseline = run_command(case.test_command, cwd=baseline_workspace, timeout_seconds=test_timeout)
    finally:
        shutil.rmtree(baseline_parent, ignore_errors=True)
    verification = run_command(case.test_command, cwd=workspace, timeout_seconds=test_timeout)
    changes = analyze_workspace_changes(case.source_dir, workspace)
    resolved = baseline.exit_code != 0 and verification.exit_code == 0 and not changes.test_files_changed
    if resolved:
        status = "resolved"
    elif baseline.exit_code == 0:
        status = "invalid_baseline"
    elif verification.exit_code == 0 and changes.test_files_changed:
        status = "invalid_test_modified"
    else:
        status = "unresolved"
    failure_mode = classify_failure_mode(
        baseline=baseline,
        agent=None,
        verification=verification,
        changes=changes,
        status=status,
        resolved=resolved,
    )
    return CaseRunResult(
        id=case.id,
        status=status,
        resolved=resolved,
        failure_mode=failure_mode,
        workspace=str(workspace),
        trace_jsonl=str(trace_path),
        changes=changes,
        baseline=baseline,
        agent=None,
        verification=verification,
        trace_status=_trace_status(trace_path),
    )


def prepare_workspace(case: SweStyleCase, run_root: str | Path, run_id: str) -> Path:
    root = Path(run_root).expanduser().resolve()
    workspace = root / run_id / _safe_name(case.id)
    _assert_child(root, workspace)
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        case.source_dir,
        workspace,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git", ".insightagent", ".pytest_cache"),
    )
    return workspace


def analyze_workspace_changes(source_dir: str | Path, workspace: str | Path) -> WorkspaceChanges:
    source_root = Path(source_dir).expanduser().resolve()
    workspace_root = Path(workspace).expanduser().resolve()
    source_files = _relative_files(source_root)
    workspace_files = _relative_files(workspace_root)
    source_set = set(source_files)
    workspace_set = set(workspace_files)
    added = sorted(workspace_set - source_set)
    deleted = sorted(source_set - workspace_set)
    modified = sorted(
        rel
        for rel in source_set & workspace_set
        if (source_root / rel).read_bytes() != (workspace_root / rel).read_bytes()
    )
    changed = sorted({*added, *deleted, *modified})
    test_changed = [rel for rel in changed if _is_test_file(rel)]
    source_changed = [rel for rel in changed if not _is_test_file(rel)]
    patch = build_unified_patch(source_root, workspace_root, added, deleted, modified)
    return WorkspaceChanges(
        modified_files=modified,
        added_files=added,
        deleted_files=deleted,
        test_files_changed=test_changed,
        source_files_changed=source_changed,
        patch=patch,
    )


def build_unified_patch(
    source_root: Path,
    workspace_root: Path,
    added: list[str],
    deleted: list[str],
    modified: list[str],
) -> str:
    sections: list[str] = []
    for relative_path in sorted({*added, *deleted, *modified}):
        before = [] if relative_path in added else _read_patch_lines(source_root / relative_path)
        after = [] if relative_path in deleted else _read_patch_lines(workspace_root / relative_path)
        fromfile = "/dev/null" if relative_path in added else f"a/{relative_path}"
        tofile = "/dev/null" if relative_path in deleted else f"b/{relative_path}"
        diff = list(
            difflib.unified_diff(
                before,
                after,
                fromfile=fromfile,
                tofile=tofile,
                lineterm="",
            )
        )
        if diff:
            sections.append("\n".join(diff))
    return "\n".join(sections)


def classify_failure_mode(
    *,
    baseline: CommandResult,
    agent: CommandResult | None,
    verification: CommandResult | None,
    changes: WorkspaceChanges,
    status: str,
    resolved: bool,
    agent_failure_mode: str | None = None,
) -> str:
    if status == "dry_run":
        return "not_run"
    if status == "invalid_environment":
        return "invalid_environment"
    if baseline.exit_code == 0:
        return "invalid_baseline"
    if agent_failure_mode is not None:
        return agent_failure_mode
    if agent is not None and agent.exit_code != 0:
        return agent_failure_mode or "agent_error"
    if resolved:
        return "resolved"
    if verification is None:
        return "missing_verification"
    if verification.timed_out:
        return "verification_timeout"
    if changes.test_files_changed:
        return "test_modified"
    if not changes.changed_files:
        return "no_patch"
    if changes.added_files and not changes.modified_files and not changes.deleted_files:
        return "only_added_files"
    if not changes.source_files_changed:
        return "no_source_changes"
    return "verification_failed"


def classify_invalid_baseline(baseline: CommandResult) -> str | None:
    if baseline.exit_code == 0:
        return "invalid_baseline"
    if baseline.timed_out:
        return "invalid_environment"
    if _is_pytest_command(baseline.command) and baseline.exit_code != 1:
        return "invalid_environment"
    output = f"{baseline.stdout}\n{baseline.stderr}"
    if _looks_like_pytest_collection_or_import_error(output):
        return "invalid_environment"
    return None


def _is_pytest_command(command: str) -> bool:
    return "pytest" in command


def _looks_like_pytest_collection_or_import_error(output: str) -> bool:
    markers = (
        "ImportError while loading conftest",
        "ERROR collecting",
        "ConftestImportFailure",
        "ModuleNotFoundError",
        "pytest_cmdline_parse",
        "no tests ran",
    )
    return any(marker in output for marker in markers)


def build_task_prompt(case: SweStyleCase) -> str:
    metadata_lines = []
    if case.metadata.get("repo"):
        metadata_lines.append(f"Repository: {case.metadata['repo']}")
    if case.metadata.get("base_commit"):
        metadata_lines.append(f"Base commit: {case.metadata['base_commit']}")
    if case.metadata.get("FAIL_TO_PASS"):
        metadata_lines.append(f"Fail-to-pass tests: {case.metadata['FAIL_TO_PASS']}")
    metadata_block = "\n".join(metadata_lines)
    if metadata_block:
        metadata_block = f"\nInstance metadata:\n{metadata_block}\n"
    return f"""You are solving a SWE-bench-style repository repair task.

Issue:
{case.issue}
{metadata_block}

Required workflow:
1. This is an existing repository repair. Do not create standalone demo files.
2. Your first tool call must inspect the repository with glob_search, grep_search, or read_file.
3. Identify the source file imported by the failing tests, then edit that existing source file.
4. Do not edit tests unless the issue explicitly says tests are wrong.
5. Run this exact verification command before finalizing: {case.test_command}
6. A different command is not sufficient for this evaluation.
7. Final answer must include changed files and the exact verification result.
"""


def build_subprocess_command(
    *,
    workspace: Path,
    trace_path: Path,
    task: str,
    provider: str,
    model: str | None,
    provider_timeout: int,
    max_wall_seconds: float,
    max_tool_iterations: int,
    max_output_tokens: int,
    language: str,
) -> list[str]:
    """构建显式隔离模式下的图 CLI 命令。"""
    command = [
        sys.executable,
        "-m",
        "insightagent.cli.run_task",
        "--provider",
        provider,
        "--workspace",
        str(workspace),
        "--task",
        task,
        "--permission-mode",
        "workspace-write",
        "--tool-profile",
        "coding-basic",
        "--trace-jsonl",
        str(trace_path),
        "--timeout",
        str(provider_timeout),
        "--max-wall-seconds",
        str(max_wall_seconds),
        "--max-tool-iterations",
        str(max_tool_iterations),
        "--max-output-tokens",
        str(max_output_tokens),
        "--language",
        language,
        "--no-trace",
    ]
    if model:
        command.extend(["--model", model])
    return command


def _run_graph_case(
    *,
    task: str,
    workspace: Path,
    trace_path: Path,
    provider: str,
    model: str | None,
    provider_timeout: int,
    max_wall_seconds: float,
    max_tool_iterations: int,
    max_output_tokens: int,
    language: str,
    session_id: str,
) -> tuple[CommandResult, str | None, str | None]:
    """执行图运行器，并转换为评测报告需要的稳定结果。"""
    start = time.monotonic()
    config = RuntimeConfig(
        provider=provider,
        model=model,
        timeout=provider_timeout,
        max_wall_seconds=max_wall_seconds,
        max_tool_iterations=max_tool_iterations,
        max_output_tokens=max_output_tokens,
        permission_mode="workspace-write",
        response_language=language,
    )
    try:
        outcome = graph_run_task(
            task=task,
            workspace=workspace,
            config=config,
            session_id=session_id,
            checkpoint_id=None,
            tool_profile="coding-basic",
            allowed_tools=None,
            enabled_mcp_servers=set(),
            trace_jsonl=str(trace_path),
            no_trace=True,
            mcp_config=MCPConfig(),
        )
    except asyncio.CancelledError:
        return (
            CommandResult(
                command="graph_run_task",
                exit_code=1,
                stdout="",
                stderr="graph run cancelled",
                duration_seconds=time.monotonic() - start,
            ),
            _trace_id_from_debug_trace(trace_path),
            "graph_cancelled",
        )
    except TimeoutError:
        return (
            CommandResult(
                command="graph_run_task",
                exit_code=124,
                stdout="",
                stderr="graph run timed out",
                duration_seconds=time.monotonic() - start,
                timed_out=True,
            ),
            _trace_id_from_debug_trace(trace_path),
            "graph_timeout",
        )
    except (MCPConfigError, ModelConfigurationError):
        return (
            CommandResult(
                command="graph_run_task",
                exit_code=1,
                stdout="",
                stderr="graph initialization failed",
                duration_seconds=time.monotonic() - start,
            ),
            _trace_id_from_debug_trace(trace_path),
            "graph_initialization_error",
        )
    except Exception:
        return (
            CommandResult(
                command="graph_run_task",
                exit_code=1,
                stdout="",
                stderr="graph run failed",
                duration_seconds=time.monotonic() - start,
            ),
            _trace_id_from_debug_trace(trace_path),
            "graph_error",
        )

    state = getattr(outcome, "state", {})
    phase = state.get("phase") if isinstance(state, Mapping) else None
    failed = phase == "failed"
    timed_out = failed and _state_has_failure_kind(state, "time_budget_exceeded")
    trace_id = getattr(outcome, "trace_id", None)
    return (
        CommandResult(
            command="graph_run_task",
            exit_code=124 if timed_out else 1 if failed else 0,
            stdout=str(getattr(outcome, "final_answer", "")),
            stderr=(
                "graph run timed out"
                if timed_out
                else "graph reported a failed terminal state"
                if failed
                else ""
            ),
            duration_seconds=time.monotonic() - start,
            timed_out=timed_out,
        ),
        trace_id if isinstance(trace_id, str) else None,
        "graph_timeout" if timed_out else "graph_failed" if failed else None,
    )


def write_reports(results: list[CaseRunResult], report_root: str | Path, run_id: str) -> tuple[Path, Path]:
    report_dir = _report_directory(report_root, run_id)
    report_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = report_dir / "results.jsonl"
    md_path = report_dir / "summary.md"
    redacted_results = [_redact_case_result(result) for result in results]
    jsonl_path.write_text(
        "\n".join(json.dumps(_to_jsonable(result), ensure_ascii=False) for result in redacted_results) + "\n",
        encoding="utf-8",
    )
    md_path.write_text(render_summary(redacted_results), encoding="utf-8")
    return jsonl_path, md_path


def export_predictions(
    results: list[CaseRunResult],
    path: str | Path,
    *,
    model_name_or_path: str,
) -> Path:
    prediction_path = Path(path).expanduser().resolve()
    prediction_path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for result in results:
        lines.append(
            json.dumps(
                {
                    "instance_id": _redact_text(result.id),
                    "model_name_or_path": _redact_text(model_name_or_path),
                    "model_patch": _redact_text(result.changes.patch),
                },
                ensure_ascii=False,
            )
        )
    prediction_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return prediction_path


def _redact_case_result(result: CaseRunResult) -> CaseRunResult:
    return replace(
        result,
        id=_redact_text(result.id),
        status=_redact_text(result.status),
        failure_mode=_redact_text(result.failure_mode),
        workspace=_redact_text(result.workspace),
        trace_jsonl=_redact_text(result.trace_jsonl),
        changes=_redact_workspace_changes(result.changes),
        baseline=_redact_command_result(result.baseline),
        agent=None if result.agent is None else _redact_command_result(result.agent),
        verification=None
        if result.verification is None
        else _redact_command_result(result.verification),
        langfuse_trace_id=None
        if result.langfuse_trace_id is None
        else _redact_text(result.langfuse_trace_id),
        trace_status=_redact_text(result.trace_status),
    )


def _redact_workspace_changes(changes: WorkspaceChanges) -> WorkspaceChanges:
    return replace(
        changes,
        modified_files=[_redact_text(path) for path in changes.modified_files],
        added_files=[_redact_text(path) for path in changes.added_files],
        deleted_files=[_redact_text(path) for path in changes.deleted_files],
        test_files_changed=[_redact_text(path) for path in changes.test_files_changed],
        source_files_changed=[_redact_text(path) for path in changes.source_files_changed],
        patch=_redact_text(changes.patch),
    )


def _redact_command_result(result: CommandResult) -> CommandResult:
    return replace(
        result,
        command=_redact_text(result.command),
        stdout=_redact_text(result.stdout),
        stderr=_redact_text(result.stderr),
    )


def _redact_text(value: str) -> str:
    # Redaction markers can be longer than the secret values they replace.
    return str(sanitize_for_model_trace_and_persistence(value, max_chars=max(len(value) * 4, 1)))


def render_summary(results: list[CaseRunResult]) -> str:
    results = [_redact_case_result(result) for result in results]
    total = len(results)
    resolved = sum(1 for result in results if result.resolved)
    invalid_baselines = sum(1 for result in results if result.status == "invalid_baseline")
    invalid_environments = sum(1 for result in results if result.status == "invalid_environment")
    evaluable = [result for result in results if _is_evaluable_result(result)]
    evaluable_count = len(evaluable)
    raw_rate = (resolved / total * 100) if total else 0
    resolution_rate = (resolved / evaluable_count * 100) if evaluable_count else 0
    lines = [
        "# SWE-style Evaluation Summary",
        "",
        f"- Total cases: {total}",
        f"- Evaluable cases: {evaluable_count}",
        f"- Invalid baseline cases: {invalid_baselines}",
        f"- Invalid environment cases: {invalid_environments}",
        f"- Resolved: {resolved}",
        f"- Resolution rate: {resolution_rate:.1f}%",
        f"- Raw resolution rate: {raw_rate:.1f}%",
        "",
        "| Case | Status | Failure Mode | Baseline | Agent | Verification | Source Changes | Test Changes | Added Files | Trace | Trace Status | Langfuse Trace | Patch + | Patch - | Workspace |",
        "| --- | --- | --- | ---: | ---: | ---: | --- | --- | --- | --- | --- | --- | ---: | ---: | --- |",
    ]
    for result in results:
        agent_code = "" if result.agent is None else str(result.agent.exit_code)
        verification_code = "" if result.verification is None else str(result.verification.exit_code)
        added_lines, deleted_lines = _count_patch_delta(result.changes.patch)
        lines.append(
            "| "
            f"{result.id} | {result.status} | {result.failure_mode} | {result.baseline.exit_code} | "
            f"{agent_code} | {verification_code} | "
            f"{_format_files(result.changes.source_files_changed)} | "
            f"{_format_files(result.changes.test_files_changed)} | "
            f"{_format_files(result.changes.added_files)} | "
            f"`{result.trace_jsonl}` | "
            f"{result.trace_status} | "
            f"{result.langfuse_trace_id or ''} | "
            f"{added_lines} | {deleted_lines} | "
            f"`{result.workspace}` |"
        )
    lines.append("")
    return "\n".join(lines)


def _is_evaluable_result(result: CaseRunResult) -> bool:
    return result.status not in {"invalid_baseline", "invalid_environment", "dry_run"}


def run_process(
    command: list[str],
    cwd: str | Path,
    timeout_seconds: float | None,
    env: dict[str, str] | None = None,
) -> CommandResult:
    display_command = subprocess.list2cmdline(command)
    return _run_subprocess(
        command,
        cwd=cwd,
        timeout_seconds=timeout_seconds,
        env=env,
        shell=False,
        display_command=display_command,
    )


def _run_subprocess(
    command: str | list[str],
    *,
    cwd: str | Path,
    timeout_seconds: float | None,
    env: dict[str, str] | None,
    shell: bool,
    display_command: str,
) -> CommandResult:
    start = time.monotonic()
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        env=env,
        shell=shell,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=os.name == "posix",
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
        return CommandResult(
            command=display_command,
            exit_code=process.returncode,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=time.monotonic() - start,
        )
    except subprocess.TimeoutExpired as error:
        _terminate_process_tree(process)
        process.communicate()
        return CommandResult(
            command=display_command,
            exit_code=124,
            stdout=_decode_timeout_output(error.stdout),
            stderr=_decode_timeout_output(error.stderr),
            duration_seconds=time.monotonic() - start,
            timed_out=True,
        )


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=_PROCESS_TERMINATION_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return

    process.terminate()
    try:
        process.wait(timeout=_PROCESS_TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()


def default_project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def main() -> None:
    parser = argparse.ArgumentParser(description="Run local SWE-bench-style repair evaluations.")
    parser.add_argument("--dataset", default="tests/fixtures/swe_style/cases.jsonl")
    parser.add_argument(
        "--checkout-root",
        help="Directory containing local SWE-bench checkouts named by sanitized instance_id.",
    )
    parser.add_argument("--run-root", default="workspaces/evals/swe_style")
    parser.add_argument("--report-root", default="reports/swe_style")
    parser.add_argument("--provider", default="siliconflow")
    parser.add_argument("--model", help="Override MODEL_ID for this evaluation run.")
    parser.add_argument(
        "--prediction-model-name",
        help="Value for model_name_or_path in SWE-bench predictions.jsonl. Defaults to --model.",
    )
    parser.add_argument("--case-id", action="append", help="Run only selected case ids.")
    parser.add_argument("--limit", type=int, help="Maximum number of cases to run.")
    parser.add_argument("--run-id", help="Stable run id for workspace/report paths.")
    parser.add_argument("--analyze-run", help="Re-analyze an existing run id without calling a model provider.")
    parser.add_argument("--dry-run", action="store_true", help="Prepare workspaces and run baseline only.")
    parser.add_argument("--test-timeout", type=float, default=60.0)
    parser.add_argument("--provider-timeout", type=int, default=300)
    parser.add_argument("--max-wall-seconds", type=float, default=300.0)
    parser.add_argument("--max-tool-iterations", type=int, default=12)
    parser.add_argument("--max-output-tokens", type=int, default=4096)
    parser.add_argument("--language", default="Chinese")
    parser.add_argument(
        "--subprocess",
        action="store_true",
        help="Run the graph CLI in a child process instead of the default in-process runner.",
    )
    parser.add_argument("--fail-on-unresolved", action="store_true")
    args = parser.parse_args()

    project_root = default_project_root()
    load_dotenv_files(project_root, start_dir=project_root)
    checkout_root = Path(args.checkout_root) if args.checkout_root else None
    if checkout_root is not None and not checkout_root.is_absolute():
        checkout_root = project_root / checkout_root
    cases = load_cases(project_root / args.dataset, checkout_root=checkout_root)
    if args.case_id:
        selected = set(args.case_id)
        cases = [case for case in cases if case.id in selected]
    if args.limit is not None:
        cases = cases[: args.limit]
    if not cases:
        raise SystemExit("no cases selected")

    run_id = args.analyze_run or args.run_id or datetime.now().strftime("%Y%m%d-%H%M%S")
    results: list[CaseRunResult] = []
    for case in cases:
        if args.analyze_run:
            result = analyze_existing_run(
                case,
                run_root=project_root / args.run_root,
                report_root=project_root / args.report_root,
                run_id=run_id,
                test_timeout=args.test_timeout,
            )
        else:
            result = run_case(
                case,
                run_root=project_root / args.run_root,
                report_root=project_root / args.report_root,
                project_root=project_root,
                provider=args.provider,
                model=args.model,
                run_id=run_id,
                dry_run=args.dry_run,
                test_timeout=args.test_timeout,
                provider_timeout=args.provider_timeout,
                max_wall_seconds=args.max_wall_seconds,
                max_tool_iterations=args.max_tool_iterations,
                max_output_tokens=args.max_output_tokens,
                language=args.language,
                subprocess_mode=args.subprocess,
            )
        results.append(result)
        print(f"{case.id}: {result.status}")

    jsonl_path, summary_path = write_reports(results, project_root / args.report_root, run_id)
    predictions_path = export_predictions(
        results,
        Path(project_root / args.report_root) / run_id / "predictions.jsonl",
        model_name_or_path=args.prediction_model_name or args.model or os.environ.get("MODEL_ID", "unknown"),
    )
    print(f"results_jsonl: {jsonl_path}")
    print(f"summary_md: {summary_path}")
    print(f"predictions_jsonl: {predictions_path}")
    if args.fail_on_unresolved and any(
        _is_evaluable_result(result) and not result.resolved for result in results
    ):
        raise SystemExit(1)


def _subprocess_env(project_root: str | Path) -> dict[str, str]:
    env = dict(os.environ)
    src_dir = Path(project_root).expanduser().resolve() / "src"
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(src_dir) if not existing else f"{src_dir}{os.pathsep}{existing}"
    return env


def _trace_status(trace_path: Path) -> str:
    """只报告可读取的 JSONL 调试追踪，避免把空路径伪装成可用。"""
    try:
        if not trace_path.is_file():
            return "unavailable"
        lines = trace_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return "unavailable"
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, Mapping):
            return "available"
    return "unavailable"


def _report_directory(report_root: str | Path, run_id: str) -> Path:
    root = Path(report_root).expanduser().resolve()
    directory = root / run_id
    _assert_child(root, directory)
    return directory


def _trace_path(report_root: str | Path, run_id: str, case_id: str) -> Path:
    report_dir = _report_directory(report_root, run_id)
    path = report_dir / f"{_safe_name(case_id)}.trace.jsonl"
    _assert_child(report_dir, path)
    return path


def _trace_id_from_debug_trace(trace_path: Path) -> str | None:
    """从图调试事件中提取可选的 Langfuse trace ID。"""
    try:
        lines = trace_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None
    for line in reversed(lines):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, Mapping):
            trace_id = event.get("trace_id")
            if isinstance(trace_id, str) and trace_id:
                return trace_id
    return None


def _debug_trace_has_failure_kind(trace_path: Path, failure_kind: str) -> bool:
    try:
        lines = trace_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return False
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, Mapping) and _state_has_failure_kind(event, failure_kind):
            return True
    return False


def _state_has_failure_kind(state: object, failure_kind: str) -> bool:
    if not isinstance(state, Mapping):
        return False
    events = state.get("tool_events")
    if not isinstance(events, list):
        return False
    return any(
        isinstance(event, Mapping) and event.get("failure_kind") == failure_kind
        for event in events
    )


def _case_metadata(data: dict[str, Any]) -> dict[str, Any]:
    structural_fields = {
        "id",
        "instance_id",
        "issue",
        "problem_statement",
        "source_dir",
        "test_command",
        "verification_command",
    }
    return {key: value for key, value in data.items() if key not in structural_fields}


def _decode_timeout_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip(".-")
    return cleaned or "case"


def _assert_child(root: Path, child: Path) -> None:
    root_resolved = root.resolve()
    child_resolved = child.resolve()
    if root_resolved != child_resolved and root_resolved not in child_resolved.parents:
        raise ValueError(f"path escapes run root: {child_resolved}")


def _relative_files(root: Path) -> list[str]:
    files: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if _is_ignored_eval_file(rel):
            continue
        files.append(rel)
    return sorted(files)


def _is_ignored_eval_file(relative_path: str) -> bool:
    parts = set(relative_path.split("/"))
    if parts & {"__pycache__", ".git", ".insightagent", ".pytest_cache"}:
        return True
    return relative_path.endswith((".pyc", ".pyo"))


def _is_test_file(relative_path: str) -> bool:
    parts = relative_path.split("/")
    name = parts[-1]
    return "tests" in parts or name.startswith("test_") or name.endswith("_test.py")


def _format_files(files: list[str]) -> str:
    return ", ".join(f"`{path}`" for path in files) if files else ""


def _count_patch_delta(patch: str) -> tuple[int, int]:
    added = 0
    deleted = 0
    for line in patch.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            deleted += 1
    return added, deleted


def _read_patch_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    data = path.read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return [f"<binary file: {path.name}>\n"]
    return text.splitlines(keepends=True)


def _to_jsonable(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return {key: _to_jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {key: _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    return value


if __name__ == "__main__":
    main()
