"""Unit tests for the small-model resilience layer."""

from __future__ import annotations

import unittest

from insightagent.resilience import (
    RetryPolicy,
    ToolCallExtractor,
    build_repair_prompt,
    loads_lenient,
)
from insightagent.runtime.failure_classifier import FailureKind
from insightagent.runtime.types import ToolExecutionResult


class LenientJsonTests(unittest.TestCase):
    def test_strict_json_is_not_marked_repaired(self) -> None:
        parsed, repaired = loads_lenient('{"a": 1}')
        self.assertEqual(parsed, {"a": 1})
        self.assertFalse(repaired)

    def test_trailing_comma_is_repaired(self) -> None:
        parsed, repaired = loads_lenient('{"a": 1, "b": 2,}')
        self.assertEqual(parsed, {"a": 1, "b": 2})
        self.assertTrue(repaired)

    def test_code_fence_wrapped_json_is_repaired(self) -> None:
        parsed, repaired = loads_lenient('```json\n{"x": "y"}\n```')
        self.assertEqual(parsed, {"x": "y"})
        self.assertTrue(repaired)

    def test_trailing_prose_after_object_is_trimmed(self) -> None:
        parsed, _ = loads_lenient('{"path": "a.py"} -- done')
        self.assertEqual(parsed, {"path": "a.py"})

    def test_unrepairable_returns_none(self) -> None:
        parsed, repaired = loads_lenient("not json at all")
        self.assertIsNone(parsed)
        self.assertTrue(repaired)


class ToolCallExtractorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.extractor = ToolCallExtractor()
        self.tools = {"write_file", "execute_command", "read_file"}

    def test_extracts_explicit_protocol_tag(self) -> None:
        text = '<tool_call>{"name": "execute_command", "arguments": {"command": "ls"}}</tool_call>'
        recovered = self.extractor.extract(text, self.tools)
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].tool_call.name, "execute_command")
        self.assertEqual(recovered[0].tool_call.arguments, {"command": "ls"})
        self.assertEqual(recovered[0].source, "protocol")

    def test_extracts_fenced_tool_call(self) -> None:
        text = '```tool_call\n{"name": "write_file", "arguments": {"path": "x.py", "content": "y"}}\n```'
        recovered = self.extractor.extract(text, self.tools)
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].tool_call.name, "write_file")

    def test_code_block_becomes_write_file_with_inferred_name(self) -> None:
        text = "Save this as calculator.py:\n```python\nprint('hi')\n```"
        recovered = self.extractor.extract(text, self.tools, task="build calculator.py")
        self.assertEqual(len(recovered), 1)
        call = recovered[0].tool_call
        self.assertEqual(call.name, "write_file")
        self.assertEqual(call.arguments["path"], "calculator.py")
        self.assertIn("print('hi')", call.arguments["content"])
        self.assertEqual(recovered[0].source, "codeblock")

    def test_code_block_default_filename_when_none_mentioned(self) -> None:
        recovered = self.extractor.extract("```python\nx=1\n```", self.tools, task="do something")
        self.assertEqual(recovered[0].tool_call.arguments["path"], "main.py")

    def test_unknown_tool_in_protocol_is_ignored(self) -> None:
        text = '<tool_call>{"name": "not_a_tool", "arguments": {}}</tool_call>'
        self.assertEqual(self.extractor.extract(text, self.tools), [])

    def test_plain_prose_yields_nothing(self) -> None:
        self.assertEqual(self.extractor.extract("I will think about it.", self.tools), [])


class RetryPolicyTests(unittest.TestCase):
    def test_exponential_backoff_is_capped(self) -> None:
        policy = RetryPolicy(base_delay=1.0, max_delay=4.0, sleep=lambda _s: None)
        self.assertEqual(policy.backoff_delay(1), 1.0)
        self.assertEqual(policy.backoff_delay(2), 2.0)
        self.assertEqual(policy.backoff_delay(3), 4.0)
        self.assertEqual(policy.backoff_delay(4), 4.0)  # capped

    def test_wait_invokes_sleep_with_delay(self) -> None:
        slept: list[float] = []
        policy = RetryPolicy(base_delay=0.5, sleep=slept.append)
        delay = policy.wait(2)
        self.assertEqual(delay, 1.0)
        self.assertEqual(slept, [1.0])


class RepairPromptTests(unittest.TestCase):
    def _result(self, content: str, kind: FailureKind) -> ToolExecutionResult:
        return ToolExecutionResult(
            name="execute_command",
            arguments={},
            content=content,
            is_error=True,
            failure_kind=kind,
            retryable=False,
            repair_guidance="",
        )

    def test_mcp_unknown_tool_gets_specific_guidance(self) -> None:
        result = ToolExecutionResult(
            name="mcp_playwright_navigate",
            arguments={},
            content="KeyError: unknown tool: mcp_playwright_navigate",
            is_error=True,
            failure_kind=FailureKind.UNKNOWN_ERROR,
        )
        prompt = build_repair_prompt(result, task="open a page")
        self.assertIn("MCP server", prompt)
        self.assertIn("mcp_config.json", prompt)
        self.assertIn("not", prompt.lower())
        self.assertIn("install", prompt.lower())  # "do not install dependencies"

    def test_network_failure_says_retry_unlikely(self) -> None:
        prompt = build_repair_prompt(self._result("Could not resolve host", FailureKind.NETWORK_ERROR))
        self.assertIn("network", prompt.lower())
        self.assertIn("likely to fail again", prompt.lower())

    def test_repair_prompt_restates_goal(self) -> None:
        prompt = build_repair_prompt(self._result("boom", FailureKind.CODE_ERROR), task="Build a calculator module")
        self.assertIn("Build a calculator module", prompt)
        self.assertIn("ORIGINAL GOAL", prompt)


if __name__ == "__main__":
    unittest.main()
