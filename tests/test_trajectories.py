"""CI guard for the 12-task trajectory harness.

Asserts the measured outcome distribution matches the resilience claims:
no iteration-exhaustion deadlocks, a strong majority of completions, and a
small number of clean (explained) failures.
"""

from __future__ import annotations

import unittest

from tests.trajectories.run_trajectories import run_scenarios


class TrajectoryHarnessTests(unittest.TestCase):
    def test_distribution_meets_resilience_targets(self) -> None:
        results, _jsonl = run_scenarios()
        self.assertEqual(len(results), 12)
        counts: dict[str, int] = {}
        for traj in results:
            counts[traj.outcome] = counts.get(traj.outcome, 0) + 1

        # The deadlock / blind-retry classes must be eliminated.
        self.assertEqual(counts.get("iter_exhausted", 0), 0)
        # A clear majority of tasks complete.
        self.assertGreaterEqual(counts.get("done", 0), 8)
        # Genuinely unrecoverable tasks fail cleanly (with a text explanation).
        self.assertGreaterEqual(counts.get("failed_clean", 0), 1)
        # No trajectory may run away past its iteration cap.
        self.assertTrue(all(traj.iterations <= 12 for traj in results))

    def test_weak_model_tasks_recover_via_framework(self) -> None:
        results, _jsonl = run_scenarios()
        by_id = {traj.id: traj for traj in results}
        # The code-block-only weak model still completes the task.
        self.assertEqual(by_id["t02_codeblock_only"].outcome, "done")
        self.assertTrue(by_id["t02_codeblock_only"].recovered)
        # The text-protocol weak model still completes the task.
        self.assertEqual(by_id["t03_text_protocol"].outcome, "done")

    def test_legacy_control_group_is_strictly_worse(self) -> None:
        legacy, _ = run_scenarios(resilience_enabled=False)
        resilient, _ = run_scenarios(resilience_enabled=True)
        legacy_counts: dict[str, int] = {}
        for traj in legacy:
            legacy_counts[traj.outcome] = legacy_counts.get(traj.outcome, 0) + 1
        resilient_counts: dict[str, int] = {}
        for traj in resilient:
            resilient_counts[traj.outcome] = resilient_counts.get(traj.outcome, 0) + 1

        # The legacy mode reproduces the iteration-exhaustion deadlocks.
        self.assertGreater(legacy_counts.get("iter_exhausted", 0), 0)
        # Resilient mode eliminates them and completes more tasks.
        self.assertEqual(resilient_counts.get("iter_exhausted", 0), 0)
        self.assertGreater(resilient_counts.get("done", 0), legacy_counts.get("done", 0))
        # The weak-model tasks specifically fail under legacy but pass under resilient.
        legacy_by_id = {t.id: t for t in legacy}
        self.assertEqual(legacy_by_id["t02_codeblock_only"].outcome, "iter_exhausted")
        self.assertEqual(legacy_by_id["t03_text_protocol"].outcome, "iter_exhausted")


if __name__ == "__main__":
    unittest.main()
