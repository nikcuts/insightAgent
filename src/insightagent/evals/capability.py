"""Deterministic capability and observability checks for the runtime."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ..graph.observability import sanitize_for_model_trace_and_persistence
from ..graph.retry import RetryPolicy
from ..graph.tools import ToolRuntime
from ..runtime.tool_context import ToolContext


@dataclass(frozen=True)
class CapabilityCase:
    id: str
    description: str
    permission_mode: str
    tool: str
    arguments: dict[str, object]
    expected_outcome: str
    expected_failure_kind: str | None = None
    repeat: int = 1


@dataclass(frozen=True)
class CapabilityResult:
    id: str
    passed: bool
    observed_outcome: str
    failure_kind: str | None
    suppressed: bool
    repeat_count: int
    content: str


def default_cases() -> tuple[CapabilityCase, ...]:
    """Return the fixed, network-free capability matrix used in reports."""
    return (
        CapabilityCase(
            "read_allowed",
            "Read an existing file in read-only mode",
            "read-only",
            "read_file",
            {"path": "README.md"},
            "allowed",
        ),
        CapabilityCase(
            "write_denied_read_only",
            "Write a file while the workspace is read-only",
            "read-only",
            "write_file",
            {"path": "blocked.txt", "content": "blocked\n"},
            "denied",
            "permission_denied",
        ),
        CapabilityCase(
            "install_denied_read_only",
            "Install a dependency while the workspace is read-only",
            "read-only",
            "execute_command",
            {"command": "python -m pip install example"},
            "denied",
            "permission_denied",
        ),
        CapabilityCase(
            "destructive_denied",
            "Run a destructive shell command",
            "workspace-write",
            "execute_command",
            {"command": "rm -rf victim"},
            "denied",
            "permission_denied",
        ),
        CapabilityCase(
            "path_escape_denied",
            "Read outside the workspace boundary",
            "read-only",
            "read_file",
            {"path": "../outside.txt"},
            "denied",
            "permission_denied",
        ),
        CapabilityCase(
            "workspace_write_allowed",
            "Create a file in workspace-write mode",
            "workspace-write",
            "write_file",
            {"path": "allowed.txt", "content": "allowed\n"},
            "allowed",
        ),
        CapabilityCase(
            "command_allowed_workspace_write",
            "Run a non-destructive command in workspace-write mode",
            "workspace-write",
            "execute_command",
            {"command": f'{sys.executable} -c "print(\'ok\')"'},
            "allowed",
        ),
        CapabilityCase(
            "unknown_tool_denied",
            "Reject a tool outside the declared capability surface",
            "workspace-write",
            "unregistered_tool",
            {},
            "failed",
            "tool_protocol_error",
        ),
        CapabilityCase(
            "repeated_denial_suppressed",
            "Suppress repeated requests for a permanently denied action",
            "read-only",
            "write_file",
            {"path": "repeated.txt", "content": "blocked\n"},
            "denied",
            "permission_denied",
            repeat=2,
        ),
    )


def run_suite(workspace: str | Path | None = None) -> dict[str, Any]:
    """Run the matrix in isolated directories and return JSON-safe evidence."""
    temporary_directory: tempfile.TemporaryDirectory[str] | None = None
    if workspace is None:
        temporary_directory = tempfile.TemporaryDirectory(prefix="insightagent-capability-")
        root = Path(temporary_directory.name)
    else:
        root = Path(workspace).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)

    try:
        results = [_run_case(case, root / case.id) for case in default_cases()]
        redaction = _run_redaction_check()
        reliability = _run_reliability_suite(root / "reliability")
        passed = sum(result.passed for result in results)
        security_cases = [
            result
            for case, result in zip(default_cases(), results, strict=True)
            if case.expected_outcome == "denied"
        ]
        return {
            "schema_version": 1,
            "suite": "capability-and-observability",
            "total": len(results),
            "passed": passed,
            "failed": len(results) - passed,
            "security_denial_rate": round(
                sum(result.observed_outcome == "denied" for result in security_cases)
                / len(security_cases),
                3,
            ),
            "redaction_passed": redaction["passed"],
            "reliability_passed": reliability["passed"] == reliability["total"],
            "results": [asdict(result) for result in results],
            "redaction": redaction,
            "reliability": reliability,
        }
    finally:
        if temporary_directory is not None:
            temporary_directory.cleanup()


def _run_case(case: CapabilityCase, workspace: Path) -> CapabilityResult:
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "README.md").write_text("InsightAgent capability fixture\n", encoding="utf-8")
    runtime = ToolRuntime(ToolContext(workspace=workspace, permission_mode=case.permission_mode))
    result = runtime.invoke(case.tool, case.arguments, remaining_seconds=10.0)
    for _ in range(1, case.repeat):
        result = runtime.invoke(case.tool, case.arguments, remaining_seconds=10.0)
    observed_outcome = "allowed" if not result.is_error else "denied" if result.failure_kind == "permission_denied" else "failed"
    failure_kind = _failure_value(result.failure_kind)
    passed = observed_outcome == case.expected_outcome and (
        case.expected_failure_kind is None or failure_kind == case.expected_failure_kind
    )
    if case.repeat > 1:
        passed = passed and result.suppressed and result.repeat_count == case.repeat - 1
    return CapabilityResult(
        id=case.id,
        passed=passed,
        observed_outcome=observed_outcome,
        failure_kind=failure_kind,
        suppressed=result.suppressed,
        repeat_count=result.repeat_count,
        content=result.content.replace(str(runtime.context.workspace), "<workspace>"),
    )


def _run_redaction_check() -> dict[str, object]:
    raw = {
        "command": "curl -H 'Authorization: Bearer abc123' https://example.test",
        "api_key": "sk-test-secret-value",
        "workspace": "/Users/private/project",
    }
    sanitized = sanitize_for_model_trace_and_persistence(raw)
    serialized = json.dumps(sanitized, ensure_ascii=False)
    passed = "abc123" not in serialized and "sk-test-secret-value" not in serialized
    return {"passed": passed, "serialized": serialized}


def _run_reliability_suite(workspace: Path) -> dict[str, object]:
    workspace.mkdir(parents=True, exist_ok=True)
    runtime = ToolRuntime(ToolContext(workspace=workspace))
    runtime._retry_policy = RetryPolicy(  # type: ignore[attr-defined]
        base_delay=0.0, max_attempts=2, sleep=lambda _delay: None
    )
    network_command = (
        f'{sys.executable} -c "import sys; '
        "sys.stderr.write('connection refused\\n'); sys.exit(1)\""
    )
    attempts = [
        runtime.invoke("execute_command", {"command": network_command}, remaining_seconds=5.0)
        for _ in range(3)
    ]
    retry_passed = (
        all(_failure_value(result.failure_kind) == "network_error" for result in attempts)
        and attempts[1].suppressed is False
        and attempts[2].suppressed is True
        and attempts[2].repeat_count == 1
    )

    timeout_command = f'{sys.executable} -c "import time; time.sleep(2)"'
    timeout_result = runtime.invoke(
        "execute_command",
        {"command": timeout_command, "timeout": 2},
        remaining_seconds=0.1,
    )
    timeout_passed = _failure_value(timeout_result.failure_kind) == "time_budget_exceeded"
    results = [
        {"id": "bounded_network_retry", "passed": retry_passed},
        {"id": "turn_budget_prevents_execution", "passed": timeout_passed},
    ]
    return {"total": len(results), "passed": sum(item["passed"] for item in results), "results": results}


def _failure_value(value: object) -> str | None:
    if value is None:
        return None
    return str(getattr(value, "value", value))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("reports/capability/latest.json"))
    args = parser.parse_args(argv)
    report = run_suite()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("suite", "total", "passed", "failed", "security_denial_rate", "redaction_passed", "reliability_passed")}, ensure_ascii=False))
    return 0 if report["failed"] == 0 and report["redaction_passed"] and report["reliability_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
