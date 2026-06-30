from __future__ import annotations

import unittest

from insightagent.api.providers import StreamAccumulator, ToolArgumentsParseError


class StreamAccumulatorTests(unittest.TestCase):
    def test_accumulates_content_and_invokes_on_delta(self) -> None:
        pieces: list[str] = []
        acc = StreamAccumulator()

        acc.add({"choices": [{"delta": {"content": "Hel"}}]}, on_delta=pieces.append)
        acc.add({"choices": [{"delta": {"content": "lo"}}]}, on_delta=pieces.append)
        response = acc.build_response()

        self.assertEqual(response.content, "Hello")
        self.assertEqual(pieces, ["Hel", "lo"])
        self.assertEqual(response.tool_calls, [])

    def test_accumulates_tool_call_across_chunks(self) -> None:
        acc = StreamAccumulator()

        acc.add(
            {
                "choices": [
                    {"delta": {"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "write_file", "arguments": ""}}]}}
                ]
            }
        )
        acc.add({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"path": '}}]}}]})
        acc.add({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"a.py"}'}}]}}]})
        response = acc.build_response()

        self.assertEqual(len(response.tool_calls), 1)
        call = response.tool_calls[0]
        self.assertEqual(call.id, "call_1")
        self.assertEqual(call.name, "write_file")
        self.assertEqual(call.arguments, {"path": "a.py"})

    def test_captures_usage_from_final_chunk(self) -> None:
        acc = StreamAccumulator()

        acc.add({"choices": [{"delta": {"content": "hi"}}]})
        acc.add({"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 3}})
        response = acc.build_response()

        self.assertIsNotNone(response.usage)
        self.assertEqual(response.usage.input_tokens, 12)
        self.assertEqual(response.usage.output_tokens, 3)

    def test_malformed_streamed_arguments_raise(self) -> None:
        acc = StreamAccumulator()
        acc.add(
            {
                "choices": [
                    {"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "write_file", "arguments": "{not json"}}]}}
                ]
            }
        )

        with self.assertRaises(ToolArgumentsParseError):
            acc.build_response()


if __name__ == "__main__":
    unittest.main()
