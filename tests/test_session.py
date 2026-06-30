from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from insightagent.api.messages import Message, ToolCall
from insightagent.agent.session import SessionStore, render_transcript


class SessionTests(unittest.TestCase):
    def test_saves_loads_and_exports_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory) / "sessions")
            session = store.create(metadata={"model": "test"})
            session.messages = [
                Message(role="user", content="hello"),
                Message(
                    role="assistant",
                    content="using a tool",
                    tool_calls=[ToolCall(id="call_1", name="read_file", arguments={"path": "a.txt"})],
                ),
                Message(role="tool", content="file data", tool_call_id="call_1"),
            ]

            store.save(session)
            loaded = store.load(session.session_id)
            export_path = store.export_markdown(loaded, Path(directory) / "transcript.md")

            self.assertEqual(loaded.session_id, session.session_id)
            self.assertEqual(len(loaded.messages), 3)
            self.assertIn(session.session_id, store.list_sessions())
            self.assertIn("read_file", render_transcript(loaded))
            self.assertTrue(export_path.is_file())


if __name__ == "__main__":
    unittest.main()
