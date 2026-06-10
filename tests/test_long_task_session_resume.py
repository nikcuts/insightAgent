"""Long-task persistence tests: session save/resume, transcripts, and multi-turn builds."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from insightagent.agent import CodeAgent
from insightagent.messages import Message, ModelResponse, ToolCall
from insightagent.providers import ModelClient
from insightagent.session import SessionStore
from insightagent.task_state import TaskPhase
from insightagent.tool_context import ToolContext
from insightagent.tools import ToolRegistry


class ScriptedModelClient(ModelClient):
    def __init__(self, responses: list[ModelResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[list[Message]] = []

    def complete(self, messages: list[Message], tools: list[dict[str, Any]]) -> ModelResponse:
        self.calls.append(list(messages))
        return self.responses.pop(0)


def tool_response(call_id: str, name: str, arguments: dict[str, Any]) -> ModelResponse:
    return ModelResponse(tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)])


class SessionResumeTests(unittest.TestCase):
    def test_long_task_persists_session_and_resumes_in_new_agent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = SessionStore(workspace / ".insightagent" / "sessions")
            session = store.create(metadata={"workspace": str(workspace)})
            tools = ToolRegistry(context=ToolContext(workspace=workspace))

            # Turn 1: build and verify part one of the project.
            first_client = ScriptedModelClient(
                [
                    tool_response("call_w1", "write_file", {"path": "part1.py", "content": "print('part one ready')\n"}),
                    tool_response(
                        "call_run1",
                        "execute_command",
                        {"command": "python3 part1.py", "cwd": str(workspace)},
                    ),
                    ModelResponse(content="part one done"),
                ]
            )
            first_agent = CodeAgent(first_client, tools=tools, session_store=store, session=session)
            first_result = first_agent.run_turn("Build part one")

            self.assertEqual(first_result.content, "part one done")
            self.assertTrue(store.path_for(session.session_id).is_file())

            persisted = store.load(session.session_id)
            self.assertEqual(persisted.metadata["task_state"]["phase"], "done")
            self.assertEqual(persisted.metadata["usage"]["turns"], 3)
            self.assertEqual(len(persisted.messages), len(first_agent.messages))

            # Turn 2: a brand-new agent resumes from the persisted session.
            second_client = ScriptedModelClient(
                [
                    tool_response("call_r1", "read_file", {"path": "part1.py"}),
                    tool_response("call_w2", "write_file", {"path": "part2.py", "content": "print('part two ready')\n"}),
                    ModelResponse(content="part two done"),
                ]
            )
            second_agent = CodeAgent(second_client, tools=tools, session_store=store, session=persisted)

            # Resumed history is intact: original system prompt plus the first turn.
            self.assertEqual(second_agent.messages[0].role, "system")
            self.assertTrue(any("part one done" in message.content for message in second_agent.messages))

            second_result = second_agent.run_turn("Now build part two")

            self.assertEqual(second_result.content, "part two done")
            self.assertTrue((workspace / "part2.py").is_file())

            # The resumed agent saw the first turn's context in its model requests.
            self.assertTrue(
                any("part one done" in message.content for message in second_client.calls[0])
            )

            reloaded = store.load(session.session_id)
            self.assertEqual(reloaded.metadata["task_state"]["phase"], "done")
            self.assertTrue(any("part two done" in message.content for message in reloaded.messages))
            self.assertEqual(store.list_sessions(), [session.session_id])

    def test_transcript_export_covers_whole_long_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = SessionStore(workspace / "sessions")
            session = store.create()
            client = ScriptedModelClient(
                [
                    tool_response("call_w", "write_file", {"path": "app.py", "content": "print('app')\n"}),
                    tool_response(
                        "call_run",
                        "execute_command",
                        {"command": "python3 app.py", "cwd": str(workspace)},
                    ),
                    ModelResponse(content="app verified"),
                ]
            )
            agent = CodeAgent(
                client,
                tools=ToolRegistry(context=ToolContext(workspace=workspace)),
                session_store=store,
                session=session,
            )
            agent.run_turn("Build and verify app.py")

            transcript_path = store.export_markdown(session, workspace / "transcript.md")
            transcript = transcript_path.read_text(encoding="utf-8")

            self.assertIn(f"# InsightAgent Session {session.session_id}", transcript)
            self.assertIn("Tool calls:", transcript)
            self.assertIn("`write_file`", transcript)
            self.assertIn("`execute_command`", transcript)
            self.assertIn("app verified", transcript)
            # Tool results appear with their call ids, so failures stay auditable later.
            self.assertIn("tool_call_id: `call_run`", transcript)


class MultiTurnProjectTests(unittest.TestCase):
    def test_multi_turn_build_resets_task_state_each_turn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            client = ScriptedModelClient(
                [
                    # Turn 1: implement and verify.
                    tool_response("call_w1", "write_file", {"path": "lib.py", "content": "GREETING = 'hi'\n"}),
                    tool_response(
                        "call_run1",
                        "execute_command",
                        {"command": "python3 -c \"import lib; print(lib.GREETING)\"", "cwd": str(workspace)},
                    ),
                    ModelResponse(content="turn one shipped"),
                    # Turn 2: extend and verify again.
                    tool_response(
                        "call_e2",
                        "edit_file",
                        {"path": "lib.py", "old": "GREETING = 'hi'", "new": "GREETING = 'hello'"},
                    ),
                    tool_response(
                        "call_run2",
                        "execute_command",
                        {"command": "python3 -c \"import lib; print(lib.GREETING)\"", "cwd": str(workspace)},
                    ),
                    ModelResponse(content="turn two shipped"),
                ]
            )
            agent = CodeAgent(
                client,
                tools=ToolRegistry(context=ToolContext(workspace=workspace)),
                max_tool_iterations=10,
            )

            first = agent.run_turn("Create the library")
            self.assertEqual(first.content, "turn one shipped")
            self.assertEqual(agent.task_state.phase, TaskPhase.DONE)
            first_verifications = agent.task_state.verification_attempts

            second = agent.run_turn("Update the greeting")
            self.assertEqual(second.content, "turn two shipped")
            self.assertEqual(agent.task_state.phase, TaskPhase.DONE)
            # Task state was rebuilt for the second turn rather than carried over.
            self.assertEqual(agent.task_state.verification_attempts, first_verifications)
            self.assertEqual((workspace / "lib.py").read_text(encoding="utf-8"), "GREETING = 'hello'\n")

            # Each turn starts back in the planning phase.
            turn_one_first_call = client.calls[0]
            turn_two_first_call = client.calls[3]
            for request in (turn_one_first_call, turn_two_first_call):
                self.assertTrue(
                    any(
                        message.role == "user" and "Current task phase: plan" in message.content
                        for message in request
                    )
                )


if __name__ == "__main__":
    unittest.main()
