"""LangChain 工具运行时的策略与超时边界测试。"""

from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
import time
import textwrap
from pathlib import Path

import pytest
from langchain_core.tools import BaseTool
from pydantic import ValidationError

from insightagent.graph.retry import RetryPolicy
from insightagent.graph import tools as graph_tools
from insightagent.graph.tools import ToolRuntime, build_builtin_tools
from insightagent.runtime.failure_classifier import FailureKind
from insightagent.runtime.tool_context import ToolContext
from insightagent.tools.execution_tools import ExecuteCommandTool


def test_builtin_tools_are_typed_langchain_tools(tmp_path: Path) -> None:
    tools = build_builtin_tools(ToolContext(workspace=tmp_path))

    assert tools
    assert all(isinstance(tool, BaseTool) for tool in tools)
    assert {tool.name for tool in tools} == {
        "edit_file",
        "execute_command",
        "find_dependencies",
        "get_code_metrics",
        "get_function_signature",
        "git_diff",
        "git_status",
        "glob_search",
        "grep_search",
        "lsp_diagnostics",
        "parse_ast",
        "read_file",
        "run_verification",
        "todo_write",
        "write_file",
    }
    assert all(tool.args_schema is not None for tool in tools)

    payload = next(tool for tool in tools if tool.name == "write_file").invoke(
        {"path": "a.py", "content": "x = 1\n"}
    )

    assert payload["is_error"] is False
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "x = 1\n"


def test_structured_parse_ast_preserves_python_structure(tmp_path: Path) -> None:
    source = tmp_path / "package" / "sample.py"
    source.parent.mkdir()
    source.write_text(
        textwrap.dedent(
            '''
            import os
            from pathlib import Path

            VALUE = 42

            class Worker(BaseWorker):
                def run(self, item: str) -> int:
                    return len(item)

            async def build(name: str) -> Worker:
                return Worker()
            '''
        ).lstrip(),
        encoding="utf-8",
    )
    tool = next(
        tool
        for tool in build_builtin_tools(ToolContext(workspace=tmp_path))
        if tool.name == "parse_ast"
    )

    result = json.loads(tool.invoke({"path": "package/sample.py"})["content"])

    assert result["file"] == "package/sample.py"
    assert [item["module"] for item in result["imports"]] == ["os", "pathlib"]
    assert result["classes"][0]["methods"][0]["name"] == "run"
    assert result["functions"][0]["name"] == "build"
    assert result["functions"][0]["is_async"] is True
    assert result["global_variables"][0]["name"] == "VALUE"


def test_structured_function_signature_disambiguates_methods(tmp_path: Path) -> None:
    (tmp_path / "sample.py").write_text(
        textwrap.dedent(
            '''
            class SetupState:
                def __init__(self, app):
                    self.app = app

            class Blueprint:
                async def __init__(self, name: str, count: int = 1) -> None:
                    self.name = name
            '''
        ).lstrip(),
        encoding="utf-8",
    )
    tool = next(
        tool
        for tool in build_builtin_tools(ToolContext(workspace=tmp_path))
        if tool.name == "get_function_signature"
    )

    ambiguous = json.loads(
        tool.invoke({"path": "sample.py", "function_name": "__init__"})["content"]
    )
    blueprint = json.loads(
        tool.invoke(
            {
                "path": "sample.py",
                "function_name": "__init__",
                "class_name": "Blueprint",
            }
        )["content"]
    )

    assert [item["class_name"] for item in ambiguous["matches"]] == [
        "SetupState",
        "Blueprint",
    ]
    assert blueprint["class_name"] == "Blueprint"
    assert blueprint["signature"] == "async def __init__(self, name: str, count: int = 1) -> None"


def test_structured_dependency_analysis_classifies_imports(tmp_path: Path) -> None:
    (tmp_path / "local_module.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "sample.py").write_text(
        "import json\nimport requests\nimport local_module\nfrom . import sibling\nfrom pathlib import Path\n",
        encoding="utf-8",
    )
    tool = next(
        tool
        for tool in build_builtin_tools(ToolContext(workspace=tmp_path))
        if tool.name == "find_dependencies"
    )

    result = json.loads(tool.invoke({"path": "sample.py"})["content"])

    assert {"json", "pathlib"}.issubset(result["stdlib"])
    assert "requests" in result["third_party"]
    assert "local_module" in result["local"]
    assert "sibling" in result["relative"]


def test_structured_code_metrics_counts_basic_metrics(tmp_path: Path) -> None:
    (tmp_path / "sample.py").write_text(
        "import os\n# comment\n\nclass Worker:\n    pass\n\ndef build():\n    return Worker()\n",
        encoding="utf-8",
    )
    tool = next(
        tool
        for tool in build_builtin_tools(ToolContext(workspace=tmp_path))
        if tool.name == "get_code_metrics"
    )

    result = json.loads(tool.invoke({"path": "sample.py"})["content"])

    assert result == {
        "file": "sample.py",
        "total_lines": 8,
        "blank_lines": 2,
        "comment_lines": 1,
        "imports": 1,
        "classes": 1,
        "functions": 1,
    }


@pytest.mark.parametrize(
    ("path", "source"),
    [
        ("missing.py", None),
        ("not-python.txt", "plain text\n"),
        ("broken.py", "def broken(:\n"),
    ],
)
def test_structured_ast_tools_report_source_loading_failures_as_errors(
    tmp_path: Path, path: str, source: str | None
) -> None:
    if source is not None:
        (tmp_path / path).write_text(source, encoding="utf-8")
    tools = {tool.name: tool for tool in build_builtin_tools(ToolContext(workspace=tmp_path))}
    arguments = {
        "parse_ast": {"path": path},
        "get_function_signature": {"path": path, "function_name": "target"},
        "find_dependencies": {"path": path},
        "get_code_metrics": {"path": path},
    }

    payloads = {
        name: tools[name].invoke(tool_arguments) for name, tool_arguments in arguments.items()
    }

    assert all(payload["is_error"] is True for payload in payloads.values())
    assert all(payload["failure_kind"] is not None for payload in payloads.values())


def test_structured_tool_uses_the_langchain_remaining_time_budget(
    tmp_path: Path,
) -> None:
    command = f'{sys.executable} -c "import time; time.sleep(1)"'
    tool = next(
        tool
        for tool in build_builtin_tools(ToolContext(workspace=tmp_path))
        if tool.name == "execute_command"
    )

    payload = tool.invoke(
        {"command": command, "timeout": 30},
        config={"configurable": {"remaining_seconds": 0.05}},
    )

    assert payload["is_error"] is True
    assert payload["failure_kind"] == "time_budget_exceeded"


def test_structured_tool_rejects_a_null_timeout(tmp_path: Path) -> None:
    tool = next(
        tool
        for tool in build_builtin_tools(ToolContext(workspace=tmp_path))
        if tool.name == "execute_command"
    )

    with pytest.raises(ValidationError):
        tool.invoke({"command": "true", "timeout": None})


def test_structured_tool_rejects_a_coerced_timeout(tmp_path: Path) -> None:
    tool = next(
        tool
        for tool in build_builtin_tools(ToolContext(workspace=tmp_path))
        if tool.name == "execute_command"
    )

    with pytest.raises(ValidationError):
        tool.invoke({"command": "true", "timeout": "1"})


def test_structured_tool_payload_exposes_runtime_policy_fields(tmp_path: Path) -> None:
    tool = next(
        tool
        for tool in build_builtin_tools(ToolContext(workspace=tmp_path))
        if tool.name == "execute_command"
    )

    payload = tool.invoke({"command": "true"})

    assert {
        "permission",
        "risk",
        "command_kind",
        "repair_guidance",
        "suppressed",
        "repeat_count",
    }.issubset(payload)


def test_zero_budget_structured_command_keeps_its_policy_metadata(
    tmp_path: Path,
) -> None:
    tool = next(
        tool
        for tool in build_builtin_tools(ToolContext(workspace=tmp_path))
        if tool.name == "execute_command"
    )

    payload = tool.invoke(
        {"command": "true"}, config={"configurable": {"remaining_seconds": 0}}
    )

    assert payload["failure_kind"] == "time_budget_exceeded"
    assert payload["permission"] == "execute"
    assert payload["risk"] == "high"


def test_structured_todo_write_serializes_nested_pydantic_models(
    tmp_path: Path,
) -> None:
    tool = next(
        tool
        for tool in build_builtin_tools(ToolContext(workspace=tmp_path))
        if tool.name == "todo_write"
    )

    payload = tool.invoke({"todos": [{"content": "检查实现", "status": "pending"}]})

    assert payload["is_error"] is False
    assert payload["arguments"] == {
        "todos": [{"content": "检查实现", "status": "pending"}]
    }
    assert (tmp_path / ".insightagent" / "todos.json").is_file()


def test_structured_todo_write_rejects_unknown_nested_fields(tmp_path: Path) -> None:
    tool = next(
        tool
        for tool in build_builtin_tools(ToolContext(workspace=tmp_path))
        if tool.name == "todo_write"
    )

    with pytest.raises(ValidationError):
        tool.invoke(
            {"todos": [{"content": "检查实现", "status": "pending", "unexpected": "x"}]}
        )


def test_read_only_policy_returns_model_visible_permission_error(
    tmp_path: Path,
) -> None:
    runtime = ToolRuntime(ToolContext(workspace=tmp_path, permission_mode="read-only"))

    result = runtime.invoke(
        "write_file", {"path": "a.py", "content": "x = 1\n"}, remaining_seconds=5.0
    )

    assert result.is_error is True
    assert "PermissionDenied" in result.content


def test_read_only_tool_result_is_deduplicated(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    runtime = ToolRuntime(ToolContext(workspace=tmp_path))

    first = runtime.invoke("read_file", {"path": "a.py"}, remaining_seconds=5.0)
    second = runtime.invoke("read_file", {"path": "a.py"}, remaining_seconds=5.0)

    assert first.is_error is False
    assert second.suppressed is True
    assert second.metadata["duplicate"] is True


def test_permanent_read_failure_is_suppressed_after_the_first_attempt(
    tmp_path: Path,
) -> None:
    runtime = ToolRuntime(ToolContext(workspace=tmp_path))

    first = runtime.invoke("read_file", {"path": "missing.py"}, remaining_seconds=5.0)
    second = runtime.invoke("read_file", {"path": "missing.py"}, remaining_seconds=5.0)

    assert first.is_error is True
    assert second.suppressed is True


def test_workspace_violation_is_permission_denied_and_is_suppressed(
    tmp_path: Path,
) -> None:
    runtime = ToolRuntime(ToolContext(workspace=tmp_path))

    first = runtime.invoke(
        "read_file", {"path": "../outside.py"}, remaining_seconds=5.0
    )
    second = runtime.invoke(
        "read_file", {"path": "../outside.py"}, remaining_seconds=5.0
    )

    assert first.failure_kind == FailureKind.PERMISSION_DENIED
    assert second.suppressed is True


def test_inner_shell_timeout_is_classified_as_timeout(tmp_path: Path) -> None:
    runtime = ToolRuntime(ToolContext(workspace=tmp_path))
    command = f'{sys.executable} -c "import time; time.sleep(2)"'

    result = runtime.invoke(
        "execute_command", {"command": command, "timeout": 1}, remaining_seconds=5.0
    )

    assert result.failure_kind == FailureKind.TIMEOUT


def test_shell_startup_error_is_returned_as_a_structured_failure(
    tmp_path: Path,
) -> None:
    runtime = ToolRuntime(ToolContext(workspace=tmp_path))

    result = runtime.invoke(
        "execute_command",
        {"command": "true", "cwd": "missing-directory"},
        remaining_seconds=5.0,
    )

    assert result.is_error is True
    assert result.failure_kind == FailureKind.ENVIRONMENT_ERROR
    assert "FileNotFoundError" in result.content


def test_retryable_failure_waits_before_the_identical_retry(tmp_path: Path) -> None:
    sleeps: list[float] = []
    runtime = ToolRuntime(ToolContext(workspace=tmp_path))
    runtime._retry_policy = RetryPolicy(
        base_delay=0.25, max_attempts=2, sleep=sleeps.append
    )
    command = (
        f'{sys.executable} -c "import sys; '
        "sys.stderr.write('connection refused\\n'); sys.exit(1)\""
    )

    first = runtime.invoke(
        "execute_command", {"command": command}, remaining_seconds=5.0
    )
    second = runtime.invoke(
        "execute_command", {"command": command}, remaining_seconds=5.0
    )

    assert first.failure_kind == FailureKind.NETWORK_ERROR
    assert second.failure_kind == FailureKind.NETWORK_ERROR
    assert sleeps == [0.25]
    assert second.metadata["backoff_delay"] == 0.25
    assert second.metadata["backoff_attempt"] == 1


def test_retry_backoff_never_outlives_the_remaining_turn_budget(tmp_path: Path) -> None:
    sleeps: list[float] = []
    runtime = ToolRuntime(ToolContext(workspace=tmp_path))
    runtime._retry_policy = RetryPolicy(
        base_delay=2.0, max_attempts=2, sleep=sleeps.append
    )
    command = (
        f'{sys.executable} -c "import sys; '
        "sys.stderr.write('connection refused\\n'); sys.exit(1)\""
    )
    runtime.invoke("execute_command", {"command": command}, remaining_seconds=5.0)

    result = runtime.invoke(
        "execute_command", {"command": command}, remaining_seconds=1.0
    )

    assert result.failure_kind == "time_budget_exceeded"
    assert sleeps == []
    assert result.metadata["backoff_delay"] == 2.0


def test_expired_budget_prevents_tool_execution(tmp_path: Path) -> None:
    runtime = ToolRuntime(ToolContext(workspace=tmp_path))

    result = runtime.invoke("read_file", {"path": "absent.py"}, remaining_seconds=0.0)

    assert result.is_error is True
    assert result.failure_kind == "time_budget_exceeded"


def test_shell_timeout_is_capped_by_remaining_turn_budget(tmp_path: Path) -> None:
    runtime = ToolRuntime(ToolContext(workspace=tmp_path))
    command = f'{sys.executable} -c "import time; time.sleep(5)"'

    result = runtime.invoke(
        "execute_command", {"command": command, "timeout": 99}, remaining_seconds=0.1
    )

    assert result.is_error is True
    assert result.failure_kind == "time_budget_exceeded"


def test_shell_timeout_equal_to_remaining_turn_budget_is_a_turn_timeout(
    tmp_path: Path,
) -> None:
    runtime = ToolRuntime(ToolContext(workspace=tmp_path))
    command = f'{sys.executable} -c "import time; time.sleep(2)"'

    result = runtime.invoke(
        "execute_command", {"command": command, "timeout": 1}, remaining_seconds=1.0
    )

    assert result.failure_kind == "time_budget_exceeded"


def test_runtime_budget_reclaims_nested_shell_process_group(tmp_path: Path) -> None:
    pids_path = tmp_path / "pids"
    child_code = "import time; time.sleep(30)"
    parent_code = (
        "import os, pathlib, subprocess, sys, time; "
        f"child = subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        f"pathlib.Path({str(pids_path)!r}).write_text(str(os.getpid()) + ' ' + str(child.pid)); "
        "time.sleep(30)"
    )
    command = f"{sys.executable} -c {parent_code!r}"
    runtime = ToolRuntime(ToolContext(workspace=tmp_path))

    try:
        result = runtime.invoke(
            "execute_command",
            {"command": command, "timeout": 30},
            remaining_seconds=0.5,
        )

        assert result.failure_kind == "time_budget_exceeded"
        parent_pid, child_pid = _wait_for_pids(pids_path)
        assert _process_eventually_gone(parent_pid)
        assert _process_eventually_gone(child_pid)
    finally:
        if pids_path.is_file():
            for pid in _wait_for_pids(pids_path):
                _kill_if_running(pid)


def test_runtime_budget_kills_sigterm_ignoring_nested_group_leader(
    tmp_path: Path,
) -> None:
    runtime = ToolRuntime(ToolContext(workspace=tmp_path))
    pid_paths: list[Path] = []

    try:
        for index in range(5):
            pid_path = tmp_path / f"ignored-{index}.pid"
            pid_paths.append(pid_path)
            ignored_leader = (
                "import os, pathlib, signal, time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                f"pathlib.Path({str(pid_path)!r}).write_text(str(os.getpid())); "
                "time.sleep(30)"
            )
            result = runtime.invoke(
                "execute_command",
                {
                    "command": f"exec {sys.executable} -c {ignored_leader!r}",
                    "timeout": 30,
                },
                remaining_seconds=0.4,
            )

            assert result.failure_kind == "time_budget_exceeded"

        for pid_path in pid_paths:
            assert _process_eventually_gone(int(pid_path.read_text(encoding="utf-8")))
    finally:
        for pid_path in pid_paths:
            if pid_path.is_file():
                _kill_if_running(int(pid_path.read_text(encoding="utf-8")))


def test_runtime_executes_shell_tools_without_a_generic_tool_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected_tool_worker(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("shell execution must be owned by the runtime process")

    monkeypatch.setattr(graph_tools, "_run_in_worker", unexpected_tool_worker)
    runtime = ToolRuntime(ToolContext(workspace=tmp_path))

    result = runtime.invoke(
        "execute_command", {"command": "true"}, remaining_seconds=5.0
    )

    assert result.is_error is False


def test_direct_command_timeout_reclaims_the_shell_process_tree(tmp_path: Path) -> None:
    child_pid_path = tmp_path / "child.pid"
    child_code = "import time; time.sleep(30)"
    parent_code = (
        "import pathlib, subprocess, sys, time; "
        f"child = subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        f"pathlib.Path({str(child_pid_path)!r}).write_text(str(child.pid)); "
        "time.sleep(30)"
    )
    command = f"{sys.executable} -c {parent_code!r}"
    tool = ExecuteCommandTool(ToolContext(workspace=tmp_path))

    try:
        with pytest.raises(subprocess.TimeoutExpired):
            tool.run({"command": command, "timeout": 1})
        child_pid = int(child_pid_path.read_text(encoding="utf-8"))
        assert _process_is_gone(child_pid)
    finally:
        if child_pid_path.is_file():
            _kill_if_running(int(child_pid_path.read_text(encoding="utf-8")))


def test_direct_timeout_kills_child_that_ignores_sigterm(tmp_path: Path) -> None:
    child_pid_path = tmp_path / "child.pid"
    child_code = (
        "import signal, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "time.sleep(30)"
    )
    parent_code = (
        "import pathlib, subprocess, sys, time; "
        f"child = subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        f"pathlib.Path({str(child_pid_path)!r}).write_text(str(child.pid)); "
        "time.sleep(30)"
    )
    command = f"{sys.executable} -c {parent_code!r}"
    tool = ExecuteCommandTool(ToolContext(workspace=tmp_path))

    try:
        with pytest.raises(subprocess.TimeoutExpired):
            tool.run({"command": command, "timeout": 1})
        child_pid = int(child_pid_path.read_text(encoding="utf-8"))
        assert _process_eventually_gone(child_pid)
    finally:
        if child_pid_path.is_file():
            _kill_if_running(int(child_pid_path.read_text(encoding="utf-8")))


def _process_is_gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError as error:
        if error.errno == errno.ESRCH:
            return True
        raise
    return False


def _kill_if_running(pid: int) -> None:
    if not _process_is_gone(pid):
        os.kill(pid, 9)
        for _ in range(20):
            if _process_is_gone(pid):
                return
            time.sleep(0.01)


def _wait_for_pids(path: Path) -> tuple[int, ...]:
    for _ in range(100):
        if path.is_file():
            return tuple(int(pid) for pid in path.read_text(encoding="utf-8").split())
        time.sleep(0.01)
    raise AssertionError(f"process did not write pid file: {path}")


def _process_eventually_gone(pid: int) -> bool:
    for _ in range(100):
        if _process_is_gone(pid):
            return True
        time.sleep(0.01)
    return False
