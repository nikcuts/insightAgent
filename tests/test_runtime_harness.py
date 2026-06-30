from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

from insightagent.agent import CodeAgent
from insightagent.messages import Message, ModelResponse, ToolCall
from insightagent.providers import ModelClient
from insightagent.resilience import RetryPolicy
from insightagent.tool_context import ToolContext
from insightagent.tools import ToolRegistry


def _no_delay_policy(max_attempts: int = 1) -> RetryPolicy:
    return RetryPolicy(max_attempts=max_attempts, sleep=lambda _seconds: None)


class FakeModelClient(ModelClient):
    def __init__(self, responses: list[ModelResponse]) -> None:
        self.responses = responses
        self.calls: list[list[Message]] = []

    def complete(self, messages: list[Message], tools: list[dict[str, Any]]) -> ModelResponse:
        self.calls.append(list(messages))
        return self.responses.pop(0)


def load_runtime_api():
    try:
        from insightagent.runtime.command_validation import CommandKind, CommandValidator
        from insightagent.runtime.failure_classifier import FailureClassifier, FailureKind
        from insightagent.runtime.permissions import PermissionEnforcer
        from insightagent.runtime.types import ToolPermission, ToolRisk, ToolSpec
        from insightagent.trace import JsonlTraceRecorder
    except ModuleNotFoundError as error:
        raise AssertionError(f"runtime harness module is missing: {error}") from error
    except ImportError as error:
        raise AssertionError(f"runtime harness API is incomplete: {error}") from error
    return (
        CommandKind,
        CommandValidator,
        FailureClassifier,
        FailureKind,
        PermissionEnforcer,
        ToolPermission,
        ToolRisk,
        ToolSpec,
        JsonlTraceRecorder,
    )


class RuntimeHarnessTests(unittest.TestCase):
    def test_command_validator_classifies_intent_and_blocks_destructive_commands(self) -> None:
        CommandKind, CommandValidator, *_ = load_runtime_api()
        validator = CommandValidator()

        self.assertEqual(validator.classify("python3 -m unittest discover -s tests").kind, CommandKind.TEST)
        self.assertEqual(validator.classify("curl https://example.com").kind, CommandKind.NETWORK)
        self.assertEqual(validator.classify("rm -rf .").kind, CommandKind.DESTRUCTIVE)

        decision = validator.validate("rm -rf .", permission_mode="workspace-write")
        self.assertFalse(decision.allowed)
        self.assertIn("destructive", decision.reason)

    def test_permission_enforcer_uses_tool_specs_before_execution(self) -> None:
        (
            _CommandKind,
            _CommandValidator,
            _FailureClassifier,
            _FailureKind,
            PermissionEnforcer,
            ToolPermission,
            ToolRisk,
            ToolSpec,
            _JsonlTraceRecorder,
        ) = load_runtime_api()
        context = ToolContext(workspace=Path.cwd(), permission_mode="read-only")
        spec = ToolSpec(
            name="write_file",
            description="write",
            input_schema={"type": "object"},
            required_permission=ToolPermission.WORKSPACE_WRITE,
            risk=ToolRisk.MEDIUM,
            mutates_workspace=True,
        )

        decision = PermissionEnforcer().check(spec, context, {"path": "x.txt"})

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.required_permission, ToolPermission.WORKSPACE_WRITE)
        self.assertIn("read-only", decision.reason)

    def test_registry_exposes_tool_specs_and_structured_execution_results(self) -> None:
        _CommandKind, _CommandValidator, _FailureClassifier, FailureKind, *_ = load_runtime_api()
        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry(context=ToolContext(workspace=Path(directory)))

            spec = registry.spec("execute_command")
            result = registry.execute(
                "execute_command",
                {
                    "command": f'"{sys.executable}" -c "raise SyntaxError(\'bad syntax\')"',
                    "cwd": str(directory),
                },
            )

        self.assertEqual(spec.name, "execute_command")
        self.assertTrue(result.is_error)
        self.assertEqual(result.failure_kind, FailureKind.CODE_ERROR)
        self.assertIn("SyntaxError", result.content)
        self.assertFalse(result.retryable)

    def test_failure_classifier_marks_network_errors_as_non_retryable_environment_failures(self) -> None:
        _CommandKind, _CommandValidator, FailureClassifier, FailureKind, *_ = load_runtime_api()
        content = "exit_code: 6\nstdout:\n\nstderr:\ncurl: (6) Could not resolve host: example.test"

        classification = FailureClassifier().classify("execute_command", content, is_error=True)

        self.assertEqual(classification.kind, FailureKind.NETWORK_ERROR)
        self.assertFalse(classification.retryable)
        self.assertIn("network", classification.repair_guidance.lower())

    def test_agent_suppresses_repeated_non_retryable_tool_calls(self) -> None:
        _CommandKind, _CommandValidator, _FailureClassifier, FailureKind, *_ = load_runtime_api()
        repeated_call = ToolCall(
            id="call_network",
            name="execute_command",
            arguments={
                "command": (
                    f'"{sys.executable}" -c "import sys; '
                    "sys.stderr.write('Could not resolve host: example.test\\n'); sys.exit(6)\""
                )
            },
        )
        client = FakeModelClient(
            [
                ModelResponse(tool_calls=[repeated_call]),
                ModelResponse(tool_calls=[ToolCall(id="call_network_again", name=repeated_call.name, arguments=repeated_call.arguments)]),
                ModelResponse(content="Stopped retrying the network failure."),
            ]
        )
        events: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory() as directory:
            agent = CodeAgent(
                client,
                # max_attempts=1 means: after the first network failure, an identical
                # retry is suppressed instead of executed again (B2 bounded backoff).
                tools=ToolRegistry(
                    context=ToolContext(workspace=Path(directory)),
                    retry_policy=_no_delay_policy(max_attempts=1),
                ),
                max_tool_iterations=5,
            )

            result = agent.run_turn_with_trace("Try the same failing network command twice", trace=events.append)

        self.assertEqual(result.content, "Stopped retrying the network failure.")
        suppressed = [event for event in events if event["type"] == "tool_call_suppressed"]
        self.assertEqual(len(suppressed), 1)
        self.assertEqual(suppressed[0]["failure_kind"], FailureKind.NETWORK_ERROR.value)
        self.assertFalse(suppressed[0]["retryable"])

    def test_jsonl_trace_recorder_persists_structured_events(self) -> None:
        *_, JsonlTraceRecorder = load_runtime_api()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trace.jsonl"
            recorder = JsonlTraceRecorder(path)

            recorder({"type": "tool_result", "tool": "execute_command", "failure_kind": "network_error"})
            recorder.close()

            lines = path.read_text(encoding="utf-8").splitlines()

        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["type"], "tool_result")
        self.assertEqual(json.loads(lines[0])["failure_kind"], "network_error")

    def test_console_tracer_summarizes_tool_call_arguments(self) -> None:
        from insightagent.trace import ConsoleTracer

        tracer = ConsoleTracer(max_chars=2000)
        output = io.StringIO()
        event = {
            "type": "model_response",
            "iteration": 1,
            "content": "Plan: write a file.",
            "tool_calls": [
                {
                    "id": "call_1",
                    "name": "write_file",
                    "arguments": {
                        "path": "generated.py",
                        "content": "print('hello')\n" * 40,
                    },
                }
            ],
        }

        with redirect_stdout(output):
            tracer(event)

        rendered = output.getvalue()
        self.assertIn("MODEL RESPONSE", rendered)
        self.assertIn("full content sent to tool", rendered)
        self.assertIn('"chars"', rendered)


if __name__ == "__main__":
    unittest.main()
