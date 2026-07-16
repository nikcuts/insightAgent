from __future__ import annotations

import asyncio
import stat
from pathlib import Path
from typing import cast

import pytest
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict

from insightagent.graph.contracts import ContractViolation, TaskContract, extract_task_contract
from insightagent.graph.tools import (
    ContractAwareToolInvoker,
    WorkspaceChanges,
    WorkspaceSnapshotService,
)
from insightagent.runtime.tool_context import ToolContext
from insightagent.runtime.types import ToolPermission, ToolRisk, ToolSpec


class _NoArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _WriteArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    path: str
    content: str


class _CommandArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    command: str


class _RollbackFailingSnapshot:
    def __init__(self, changes: WorkspaceChanges) -> None:
        self._changes = changes
        self.special_paths: frozenset[str] = frozenset()

    def changes(self) -> WorkspaceChanges:
        return self._changes

    def restore(self) -> None:
        raise RuntimeError("restore failed")


class _RollbackFailingSnapshotService:
    def __init__(self, changes: WorkspaceChanges) -> None:
        self._snapshot = _RollbackFailingSnapshot(changes)

    def capture(self) -> _RollbackFailingSnapshot:
        return self._snapshot


def _repair_contract() -> TaskContract:
    return extract_task_contract(
        "SWE-bench repository repair task. Fix the existing implementation.\n"
        "Fail-to-pass tests: ['tests/test_calc.py::test_add']\n"
        "Run this exact verification command before finalizing: python -m pytest tests/test_calc.py -q"
    )


@pytest.mark.parametrize(
    ("tool_name", "arguments", "inspected_files", "expected"),
    [
        (
            "execute_command",
            {"command": "python -m pytest -q"},
            ["src/calc.py", "tests/test_calc.py"],
            "exact verification command",
        ),
        (
            "edit_file",
            {"path": "src/calc.py", "old": "-", "new": "+"},
            [],
            "Inspect the existing repository",
        ),
        (
            "edit_file",
            {"path": "tests/test_calc.py", "old": "-", "new": "+"},
            ["src/calc.py", "tests/test_calc.py"],
            "Do not modify test files",
        ),
    ],
)
def test_repository_repair_contract_rejects_invalid_tool_actions(
    tool_name: str,
    arguments: dict[str, object],
    inspected_files: list[str],
    expected: str,
) -> None:
    contract = _repair_contract()

    with pytest.raises(ContractViolation, match=expected):
        contract.validate_before_tool(
            tool_name,
            arguments,
            inspected_files=inspected_files,
            changed_files=[],
            verification_failed=False,
        )


def test_repository_repair_contract_enforces_test_read_patch_and_reinspection_order() -> None:
    contract = _repair_contract()
    source_edit = {"path": "src/calc.py", "old": "return a - b", "new": "return a + b"}

    with pytest.raises(ContractViolation, match="Read the fail-to-pass test body"):
        contract.validate_before_tool(
            "edit_file",
            source_edit,
            inspected_files=["src/calc.py"],
            changed_files=[],
            verification_failed=False,
        )
    contract.validate_before_tool(
        "edit_file",
        source_edit,
        inspected_files=["src/calc.py", "tests/test_calc.py"],
        changed_files=[],
        verification_failed=False,
    )
    with pytest.raises(ContractViolation, match="Inspect the latest failing verification"):
        contract.validate_before_tool(
            "edit_file",
            source_edit,
            inspected_files=["src/calc.py", "tests/test_calc.py"],
            changed_files=["src/calc.py"],
            verification_failed=True,
        )
    contract.validate_before_tool(
        "edit_file",
        source_edit,
        inspected_files=["src/calc.py", "tests/test_calc.py", "src/traceback.txt"],
        changed_files=["src/calc.py"],
        verification_failed=False,
    )


def test_repository_repair_contract_rejects_standalone_files_and_destructive_symbols() -> None:
    contract = _repair_contract()

    with pytest.raises(ContractViolation, match="standalone files"):
        contract.validate_after_tool(
            "write_file",
            {"path": "demo.py", "content": "print('demo')"},
            {"is_error": False},
            changed_files=[],
            created_files=["demo.py"],
            removed_symbols={},
        )
    with pytest.raises(ContractViolation, match="destructive Python source rewrite"):
        contract.validate_after_tool(
            "write_file",
            {"path": "src/module.py", "content": "def keep(): pass"},
            {"is_error": False},
            changed_files=["src/module.py"],
            created_files=[],
            removed_symbols={"src/module.py": ("one", "two", "three", "four")},
        )


def test_contract_aware_invoker_rolls_back_mcp_test_write_and_new_directories(
    tmp_path: Path,
) -> None:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    target = tests_dir / "test_target.py"
    original = b"def test_target():\n    assert True\n"
    target.write_bytes(original)
    target.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    original_mode = stat.S_IMODE(target.stat().st_mode)

    async def mutate_workspace() -> dict[str, object]:
        target.write_text("def test_target():\n    assert False\n", encoding="utf-8")
        nested = tmp_path / "scratch" / "nested"
        nested.mkdir(parents=True)
        (nested / "demo.py").write_text("print('demo')\n", encoding="utf-8")
        return {"ok": True}

    tool = StructuredTool(
        name="mcp_write_tests",
        description="Mutate files for a rollback test.",
        args_schema=_NoArguments,
        coroutine=mutate_workspace,
    )
    spec = ToolSpec(
        name="mcp_write_tests",
        description=tool.description,
        input_schema={},
        required_permission=ToolPermission.MCP,
        risk=ToolRisk.HIGH,
        mutates_workspace=True,
        mcp_server="fake",
    )

    async def scenario() -> None:
        invoker = ContractAwareToolInvoker(ToolContext(workspace=tmp_path))
        result = await invoker.invoke(
            tool,
            spec,
            {},
            {},
            _repair_contract(),
            inspected_files=["src/calc.py", "tests/test_calc.py"],
            changed_files=[],
            verification_failed=False,
        )
        assert result["is_error"] is True
        assert result["failure_kind"] == "task_contract"
        assert target.read_bytes() == original
        assert stat.S_IMODE(target.stat().st_mode) == original_mode
        assert not (tmp_path / "scratch").exists()

    asyncio.run(scenario())


def test_contract_aware_invoker_rolls_back_destructive_python_write(tmp_path: Path) -> None:
    module = tmp_path / "module.py"
    original = "\n".join(
        [
            "def one(): pass",
            "def two(): pass",
            "def three(): pass",
            "def four(): pass",
            "def five(): pass",
            "def six(): pass",
            "",
        ]
    )
    module.write_text(original, encoding="utf-8")

    async def write_file(path: str, content: str) -> dict[str, object]:
        (tmp_path / path).write_text(content, encoding="utf-8")
        return {"ok": True}

    tool = StructuredTool(
        name="write_file",
        description="Write a file for a rollback test.",
        args_schema=_WriteArguments,
        coroutine=write_file,
    )
    spec = ToolSpec(
        name="write_file",
        description=tool.description,
        input_schema={},
        required_permission=ToolPermission.WORKSPACE_WRITE,
        risk=ToolRisk.MEDIUM,
        mutates_workspace=True,
    )

    async def scenario() -> None:
        invoker = ContractAwareToolInvoker(ToolContext(workspace=tmp_path))
        result = await invoker.invoke(
            tool,
            spec,
            {"path": "module.py", "content": "def one(): pass\n"},
            {},
            extract_task_contract("SWE-bench repository repair task. Fix module.py."),
            inspected_files=["module.py"],
            changed_files=[],
            verification_failed=False,
        )
        assert result["failure_kind"] == "task_contract"
        assert module.read_text(encoding="utf-8") == original

    asyncio.run(scenario())


def test_contract_aware_invoker_rolls_back_mcp_test_file_deletion(tmp_path: Path) -> None:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    target = tests_dir / "test_target.py"
    original = b"def test_target():\n    assert True\n"
    target.write_bytes(original)
    target.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    original_mode = stat.S_IMODE(target.stat().st_mode)

    async def delete_test_file() -> dict[str, object]:
        target.unlink()
        return {"ok": True}

    tool = StructuredTool(
        name="mcp_delete_test",
        description="Delete a test for a rollback test.",
        args_schema=_NoArguments,
        coroutine=delete_test_file,
    )
    spec = ToolSpec(
        name="mcp_delete_test",
        description=tool.description,
        input_schema={},
        required_permission=ToolPermission.MCP,
        risk=ToolRisk.HIGH,
        mutates_workspace=True,
        mcp_server="fake",
    )

    async def scenario() -> None:
        invoker = ContractAwareToolInvoker(ToolContext(workspace=tmp_path))
        result = await invoker.invoke(
            tool,
            spec,
            {},
            {},
            _repair_contract(),
            inspected_files=["src/calc.py", "tests/test_calc.py"],
            changed_files=[],
            verification_failed=False,
        )
        assert result["failure_kind"] == "task_contract"
        assert target.read_bytes() == original
        assert stat.S_IMODE(target.stat().st_mode) == original_mode

    asyncio.run(scenario())


def test_contract_aware_invoker_rolls_back_allowed_execute_command_test_write(
    tmp_path: Path,
) -> None:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    target = tests_dir / "test_target.py"
    original = b"def test_target():\n    assert True\n"
    target.write_bytes(original)

    async def execute_command(command: str) -> dict[str, object]:
        assert command == "python -m pytest tests/test_calc.py -q"
        target.write_text("def test_target():\n    assert False\n", encoding="utf-8")
        return {"is_error": False, "content": "exit_code: 0"}

    tool = StructuredTool(
        name="execute_command",
        description="Execute a command for a rollback test.",
        args_schema=_CommandArguments,
        coroutine=execute_command,
    )
    spec = ToolSpec(
        name="execute_command",
        description=tool.description,
        input_schema={},
        required_permission=ToolPermission.EXECUTE,
        risk=ToolRisk.HIGH,
        executes_code=True,
    )

    async def scenario() -> None:
        invoker = ContractAwareToolInvoker(ToolContext(workspace=tmp_path))
        result = await invoker.invoke(
            tool,
            spec,
            {"command": "python -m pytest tests/test_calc.py -q"},
            {},
            _repair_contract(),
            inspected_files=["src/calc.py", "tests/test_calc.py"],
            changed_files=["src/calc.py"],
            verification_failed=False,
        )
        assert result["failure_kind"] == "task_contract"
        assert target.read_bytes() == original

    asyncio.run(scenario())


def test_contract_aware_invoker_removes_mcp_symlink_without_writing_its_target(
    tmp_path: Path,
) -> None:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    target = tests_dir / "test_target.py"
    original = b"def test_target():\n    assert True\n"
    target.write_bytes(original)
    outside = tmp_path / "outside.py"
    outside_original = b"OUTSIDE = True\n"
    outside.write_bytes(outside_original)

    async def replace_with_symlink() -> dict[str, object]:
        target.unlink()
        target.symlink_to(outside)
        return {"ok": True}

    tool = StructuredTool(
        name="mcp_link_test",
        description="Create a symlink for a rollback test.",
        args_schema=_NoArguments,
        coroutine=replace_with_symlink,
    )
    spec = ToolSpec(
        name="mcp_link_test",
        description=tool.description,
        input_schema={},
        required_permission=ToolPermission.MCP,
        risk=ToolRisk.HIGH,
        mutates_workspace=True,
        mcp_server="fake",
    )

    async def scenario() -> None:
        invoker = ContractAwareToolInvoker(ToolContext(workspace=tmp_path))
        result = await invoker.invoke(
            tool,
            spec,
            {},
            {},
            _repair_contract(),
            inspected_files=["src/calc.py", "tests/test_calc.py"],
            changed_files=[],
            verification_failed=False,
        )
        assert result["failure_kind"] == "task_contract"
        assert not target.is_symlink()
        assert target.read_bytes() == original
        assert outside.read_bytes() == outside_original

    asyncio.run(scenario())


def test_contract_aware_invoker_rolls_back_mcp_source_file_deletion(tmp_path: Path) -> None:
    module = tmp_path / "module.py"
    original = "\n".join(
        [
            "def one(): pass",
            "def two(): pass",
            "def three(): pass",
            "def four(): pass",
            "def five(): pass",
            "def six(): pass",
            "",
        ]
    )
    module.write_text(original, encoding="utf-8")

    async def delete_source_file() -> dict[str, object]:
        module.unlink()
        return {"ok": True}

    tool = StructuredTool(
        name="mcp_delete_source",
        description="Delete source for a rollback test.",
        args_schema=_NoArguments,
        coroutine=delete_source_file,
    )
    spec = ToolSpec(
        name="mcp_delete_source",
        description=tool.description,
        input_schema={},
        required_permission=ToolPermission.MCP,
        risk=ToolRisk.HIGH,
        mutates_workspace=True,
        mcp_server="fake",
    )

    async def scenario() -> None:
        invoker = ContractAwareToolInvoker(ToolContext(workspace=tmp_path))
        result = await invoker.invoke(
            tool,
            spec,
            {},
            {},
            extract_task_contract("SWE-bench repository repair task. Fix module.py."),
            inspected_files=["module.py"],
            changed_files=[],
            verification_failed=False,
        )
        assert result["failure_kind"] == "task_contract"
        assert module.read_text(encoding="utf-8") == original

    asyncio.run(scenario())


def test_contract_aware_invoker_rolls_back_partial_write_before_tool_exception(
    tmp_path: Path,
) -> None:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    target = tests_dir / "test_target.py"
    original = b"def test_target():\n    assert True\n"
    target.write_bytes(original)

    async def write_then_fail() -> dict[str, object]:
        target.write_text("def test_target():\n    assert False\n", encoding="utf-8")
        raise RuntimeError("remote write failed after mutation")

    tool = StructuredTool(
        name="mcp_write_then_fail",
        description="Mutate then fail for a rollback test.",
        args_schema=_NoArguments,
        coroutine=write_then_fail,
    )
    spec = ToolSpec(
        name="mcp_write_then_fail",
        description=tool.description,
        input_schema={},
        required_permission=ToolPermission.MCP,
        risk=ToolRisk.HIGH,
        mutates_workspace=True,
        mcp_server="fake",
    )

    async def scenario() -> None:
        invoker = ContractAwareToolInvoker(ToolContext(workspace=tmp_path))
        result = await invoker.invoke(
            tool,
            spec,
            {},
            {},
            _repair_contract(),
            inspected_files=["src/calc.py", "tests/test_calc.py"],
            changed_files=[],
            verification_failed=False,
        )
        assert result["failure_kind"] == "unknown_error"
        assert isinstance(result["content"], str)
        assert "RuntimeError: remote write failed after mutation" in result["content"]
        assert isinstance(result["metadata"], dict)
        assert result["metadata"]["rolled_back"] is True
        assert target.read_bytes() == original

    asyncio.run(scenario())


def test_contract_aware_invoker_restores_deleted_existing_empty_directory(tmp_path: Path) -> None:
    empty = tmp_path / "existing-empty"
    empty.mkdir()
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    target = tests_dir / "test_target.py"
    target.write_text("def test_target():\n    assert True\n", encoding="utf-8")

    async def remove_directory_and_mutate_test() -> dict[str, object]:
        empty.rmdir()
        target.write_text("def test_target():\n    assert False\n", encoding="utf-8")
        return {"ok": True}

    tool = StructuredTool(
        name="mcp_remove_empty_directory",
        description="Delete an empty directory for a rollback test.",
        args_schema=_NoArguments,
        coroutine=remove_directory_and_mutate_test,
    )
    spec = ToolSpec(
        name="mcp_remove_empty_directory",
        description=tool.description,
        input_schema={},
        required_permission=ToolPermission.MCP,
        risk=ToolRisk.HIGH,
        mutates_workspace=True,
        mcp_server="fake",
    )

    async def scenario() -> None:
        invoker = ContractAwareToolInvoker(ToolContext(workspace=tmp_path))
        result = await invoker.invoke(
            tool,
            spec,
            {},
            {},
            _repair_contract(),
            inspected_files=["src/calc.py", "tests/test_calc.py"],
            changed_files=[],
            verification_failed=False,
        )
        assert result["failure_kind"] == "task_contract"
        assert empty.is_dir()
        assert "assert True" in target.read_text(encoding="utf-8")

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("tool_name", "permission"),
    [
        ("write_file", ToolPermission.WORKSPACE_WRITE),
        ("mcp_write_then_cancel", ToolPermission.MCP),
    ],
)
def test_contract_aware_invoker_rolls_back_workspace_write_before_cancellation(
    tmp_path: Path, tool_name: str, permission: ToolPermission
) -> None:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    target = tests_dir / "test_target.py"
    original = b"def test_target():\n    assert True\n"
    target.write_bytes(original)

    async def write_then_cancel() -> dict[str, object]:
        target.write_text("def test_target():\n    assert False\n", encoding="utf-8")
        raise asyncio.CancelledError()

    tool = StructuredTool(
        name=tool_name,
        description="Mutate then cancel for a rollback test.",
        args_schema=_NoArguments,
        coroutine=write_then_cancel,
    )
    spec = ToolSpec(
        name=tool_name,
        description=tool.description,
        input_schema={},
        required_permission=permission,
        risk=ToolRisk.HIGH,
        mutates_workspace=True,
        mcp_server="fake" if permission is ToolPermission.MCP else None,
    )

    async def scenario() -> None:
        invoker = ContractAwareToolInvoker(ToolContext(workspace=tmp_path))
        with pytest.raises(asyncio.CancelledError):
            await invoker.invoke(
                tool,
                spec,
                {},
                {},
                _repair_contract(),
                inspected_files=["src/calc.py", "tests/test_calc.py"],
                changed_files=[],
                verification_failed=False,
            )
        assert target.read_bytes() == original

    asyncio.run(scenario())


def test_contract_aware_invoker_restores_file_replaced_by_directory(tmp_path: Path) -> None:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    target = tests_dir / "test_target.py"
    original = b"def test_target():\n    assert True\n"
    target.write_bytes(original)

    async def replace_file_with_directory() -> dict[str, object]:
        target.unlink()
        target.mkdir()
        (target / "nested.py").write_text("print('unsafe')\n", encoding="utf-8")
        return {"ok": True}

    tool = StructuredTool(
        name="mcp_replace_test_file",
        description="Replace a test with a directory for a rollback test.",
        args_schema=_NoArguments,
        coroutine=replace_file_with_directory,
    )
    spec = ToolSpec(
        name="mcp_replace_test_file",
        description=tool.description,
        input_schema={},
        required_permission=ToolPermission.MCP,
        risk=ToolRisk.HIGH,
        mutates_workspace=True,
        mcp_server="fake",
    )

    async def scenario() -> None:
        invoker = ContractAwareToolInvoker(ToolContext(workspace=tmp_path))
        result = await invoker.invoke(
            tool,
            spec,
            {},
            {},
            _repair_contract(),
            inspected_files=["src/calc.py", "tests/test_calc.py"],
            changed_files=[],
            verification_failed=False,
        )
        assert result["failure_kind"] == "task_contract"
        assert target.is_file()
        assert target.read_bytes() == original

    asyncio.run(scenario())


def test_cancellation_preserves_snapshot_restore_failure_as_its_cause(tmp_path: Path) -> None:
    async def cancel() -> dict[str, object]:
        raise asyncio.CancelledError()

    tool = StructuredTool(
        name="mcp_cancel",
        description="Cancel for a rollback failure test.",
        args_schema=_NoArguments,
        coroutine=cancel,
    )
    spec = ToolSpec(
        name="mcp_cancel",
        description=tool.description,
        input_schema={},
        required_permission=ToolPermission.MCP,
        risk=ToolRisk.HIGH,
        mutates_workspace=True,
        mcp_server="fake",
    )
    snapshots = cast(
        WorkspaceSnapshotService,
        _RollbackFailingSnapshotService(WorkspaceChanges((), (), (), {}, ())),
    )

    async def scenario() -> None:
        invoker = ContractAwareToolInvoker(ToolContext(workspace=tmp_path), snapshots)
        with pytest.raises(asyncio.CancelledError, match="workspace rollback failed") as caught:
            await invoker.invoke(
                tool,
                spec,
                {},
                {},
                _repair_contract(),
                inspected_files=["src/calc.py", "tests/test_calc.py"],
                changed_files=[],
                verification_failed=False,
            )
        assert isinstance(caught.value.__cause__, RuntimeError)

    asyncio.run(scenario())


def test_contract_violation_reports_rollback_failure_separately(tmp_path: Path) -> None:
    async def mutate_test() -> dict[str, object]:
        return {"ok": True}

    tool = StructuredTool(
        name="mcp_mutate_test",
        description="Return after a synthetic test mutation.",
        args_schema=_NoArguments,
        coroutine=mutate_test,
    )
    spec = ToolSpec(
        name="mcp_mutate_test",
        description=tool.description,
        input_schema={},
        required_permission=ToolPermission.MCP,
        risk=ToolRisk.HIGH,
        mutates_workspace=True,
        mcp_server="fake",
    )
    snapshots = cast(
        WorkspaceSnapshotService,
        _RollbackFailingSnapshotService(
            WorkspaceChanges(("tests/test_target.py",), (), (), {}, ())
        ),
    )

    async def scenario() -> None:
        invoker = ContractAwareToolInvoker(ToolContext(workspace=tmp_path), snapshots)
        result = await invoker.invoke(
            tool,
            spec,
            {},
            {},
            _repair_contract(),
            inspected_files=["src/calc.py", "tests/test_calc.py"],
            changed_files=[],
            verification_failed=False,
        )
        assert result["failure_kind"] == "workspace_rollback_failed"
        assert isinstance(result["metadata"], dict)
        assert result["metadata"]["original_failure_kind"] == "task_contract"
        assert result["metadata"]["rollback_error_type"] == "RuntimeError"

    asyncio.run(scenario())


def test_contract_aware_invoker_rejects_side_effects_with_existing_symlinks(
    tmp_path: Path,
) -> None:
    original = tmp_path / "original.py"
    original.write_text("VALUE = 1\n", encoding="utf-8")
    link = tmp_path / "linked.py"
    link.symlink_to(original)
    invoked = False

    async def mutate() -> dict[str, object]:
        nonlocal invoked
        invoked = True
        link.unlink()
        return {"ok": True}

    tool = StructuredTool(
        name="mcp_mutate_workspace",
        description="Attempt a mutation with an existing symlink.",
        args_schema=_NoArguments,
        coroutine=mutate,
    )
    spec = ToolSpec(
        name="mcp_mutate_workspace",
        description=tool.description,
        input_schema={},
        required_permission=ToolPermission.MCP,
        risk=ToolRisk.HIGH,
        mutates_workspace=True,
        mcp_server="fake",
    )

    async def scenario() -> None:
        invoker = ContractAwareToolInvoker(ToolContext(workspace=tmp_path))
        result = await invoker.invoke(
            tool,
            spec,
            {},
            {},
            _repair_contract(),
            inspected_files=["src/calc.py", "tests/test_calc.py"],
            changed_files=[],
            verification_failed=False,
        )
        assert result["failure_kind"] == "task_contract"
        assert invoked is False
        assert link.is_symlink()
        assert original.read_text(encoding="utf-8") == "VALUE = 1\n"

    asyncio.run(scenario())
