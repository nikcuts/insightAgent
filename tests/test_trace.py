"""控制台 / JSONL trace 渲染的回归测试。

这些用例守护一个历史 bug：当模型发出 tool_calls 时，ConsoleTracer 会因
`_summarize_arguments` 被错误地定义在另一个类上而抛出 AttributeError 崩溃。
此前的单元测试从未让 ConsoleTracer 渲染过带 tool_call 的事件，导致测试套件
全绿、真实运行却必崩。
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from insightagent.telemetry.trace import CompositeTracer, ConsoleTracer, JsonlTraceRecorder


class ConsoleTracerToolCallTests(unittest.TestCase):
    def _render(self, event: dict) -> str:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            ConsoleTracer(max_chars=2000)(event)
        return buffer.getvalue()

    def test_model_response_with_tool_calls_renders_without_error(self) -> None:
        event = {
            "type": "model_response",
            "iteration": 2,
            "content": "plan then act",
            "tool_calls": [
                {"id": "c1", "name": "write_file", "arguments": {"path": "hello.py", "content": "print(1)"}},
            ],
        }

        output = self._render(event)

        self.assertIn("MODEL RESPONSE", output)
        self.assertIn("write_file", output)
        self.assertIn("hello.py", output)

    def test_tool_call_event_renders_without_error(self) -> None:
        event = {
            "type": "tool_call",
            "id": "c1",
            "name": "execute_command",
            "arguments": {"command": "python3 hello.py"},
        }

        output = self._render(event)

        self.assertIn("TOOL CALL", output)
        self.assertIn("execute_command", output)

    def test_long_content_argument_is_summarized_not_dumped(self) -> None:
        long_content = "x" * 500  # 超长内容应被摘要展示，而非整段打印到 trace
        event = {
            "type": "tool_call",
            "id": "c1",
            "name": "write_file",
            "arguments": {"path": "big.py", "content": long_content},
        }

        output = self._render(event)

        self.assertIn("chars", output)
        self.assertIn("preview", output)
        self.assertNotIn(long_content, output)


class CompositeTracerTests(unittest.TestCase):
    def test_composite_forwards_tool_call_event_to_console_and_jsonl(self) -> None:
        event = {
            "type": "model_response",
            "iteration": 1,
            "content": "act",
            "tool_calls": [
                {"id": "c1", "name": "write_file", "arguments": {"path": "a.py", "content": "x"}},
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            jsonl_path = Path(directory) / "trace.jsonl"
            jsonl = JsonlTraceRecorder(jsonl_path)
            tracer = CompositeTracer(ConsoleTracer(max_chars=2000), jsonl)
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                tracer(event)
            tracer.close()

            console_output = buffer.getvalue()
            recorded = [json.loads(line) for line in jsonl_path.read_text(encoding="utf-8").splitlines()]

        self.assertIn("write_file", console_output)
        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0]["tool_calls"][0]["name"], "write_file")


if __name__ == "__main__":
    unittest.main()
