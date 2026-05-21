from __future__ import annotations

import unittest

from insightagent.cli import build_parser


class CliTests(unittest.TestCase):
    def test_parser_accepts_v2_context_options(self) -> None:
        parser = build_parser()

        args = parser.parse_args(
            [
                "--provider",
                "openai",
                "--workspace",
                "/tmp/work",
                "--max-tool-result-chars",
                "500",
                "--no-compact",
            ]
        )

        self.assertEqual(args.provider, "openai")
        self.assertEqual(args.workspace, "/tmp/work")
        self.assertEqual(args.max_tool_result_chars, 500)
        self.assertTrue(args.no_compact)


if __name__ == "__main__":
    unittest.main()
