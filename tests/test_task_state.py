from __future__ import annotations

import unittest

from insightagent.agent.task_state import TaskPhase, TaskState, phase_instruction, transition_after_tool


class TaskStateTests(unittest.TestCase):
    def test_initial_phase_is_plan(self) -> None:
        state = TaskState()

        self.assertEqual(state.phase, TaskPhase.PLAN)
        self.assertIn("Current task phase: plan", phase_instruction(state))

    def test_write_tool_moves_plan_to_implement(self) -> None:
        state = TaskState()

        old_phase, new_phase = transition_after_tool(state, "write_file", "wrote file", is_error=False)

        self.assertEqual(old_phase, TaskPhase.PLAN)
        self.assertEqual(new_phase, TaskPhase.IMPLEMENT)
        self.assertEqual(state.phase, TaskPhase.IMPLEMENT)

    def test_successful_verification_moves_to_summarize(self) -> None:
        state = TaskState(phase=TaskPhase.IMPLEMENT)

        old_phase, new_phase = transition_after_tool(state, "execute_command", "exit_code: 0\nstdout:\nok", is_error=False)

        self.assertEqual(old_phase, TaskPhase.IMPLEMENT)
        self.assertEqual(new_phase, TaskPhase.SUMMARIZE)
        self.assertEqual(state.verification_attempts, 1)

    def test_lsp_diagnostics_with_errors_moves_to_repair(self) -> None:
        state = TaskState(phase=TaskPhase.VERIFY)

        _old_phase, new_phase = transition_after_tool(state, "lsp_diagnostics", "bad.py: SyntaxError: invalid", is_error=False)

        self.assertEqual(new_phase, TaskPhase.REPAIR)
        self.assertEqual(state.repair_attempts, 1)
        self.assertIn("SyntaxError", state.last_error or "")

    def test_failed_tool_moves_to_repair(self) -> None:
        state = TaskState(phase=TaskPhase.IMPLEMENT)

        _old_phase, new_phase = transition_after_tool(state, "read_file", "FileNotFoundError: missing", is_error=True)

        self.assertEqual(new_phase, TaskPhase.REPAIR)
        self.assertEqual(state.repair_attempts, 1)
        self.assertIn("FileNotFoundError", state.last_error or "")

    def test_repair_limit_moves_to_failed(self) -> None:
        state = TaskState(phase=TaskPhase.REPAIR, repair_attempts=2, max_repairs=3)

        _old_phase, new_phase = transition_after_tool(state, "execute_command", "exit_code: 1", is_error=True)

        self.assertEqual(new_phase, TaskPhase.FAILED)
        self.assertEqual(state.repair_attempts, 3)


if __name__ == "__main__":
    unittest.main()
