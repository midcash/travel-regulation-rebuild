from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from .runtime import HilError, HilStore, TravelHilLoop, default_request


class HumanInLoopRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.store = HilStore(Path(self.temp_dir.name) / "hil.sqlite3")
        self.loop = TravelHilLoop(self.store)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_pause_persist_restart_and_complete(self):
        first = self.loop.start()
        self.assertEqual(first["status"], "waiting_for_human")
        self.assertEqual(first["stage"], "await_selection")
        interaction_id = first["interaction_id"]

        restarted = TravelHilLoop(HilStore(self.store.path))
        self.assertEqual(restarted.resume(first["run_id"])["interaction_id"], interaction_id)

        second = restarted.respond(
            interaction_id,
            "select",
            {"transport_id": "transport_001", "stay_id": "stay_001"},
            response_id="response-selection-1",
        )
        self.assertEqual(second["status"], "waiting_for_human")
        self.assertEqual(second["stage"], "await_approval")
        self.assertEqual(second["tool_calls"], 3)

        finished = restarted.respond(
            second["interaction_id"],
            "approve",
            response_id="response-approval-1",
        )
        self.assertEqual(finished["status"], "completed")
        self.assertEqual(finished["plan"]["total_cny"], 628)

    def test_duplicate_response_is_idempotent(self):
        first = self.loop.start()
        response = {"transport_id": "transport_001", "stay_id": "stay_001"}
        result = self.loop.respond(first["interaction_id"], "select", response, "same-response")
        repeated = self.loop.respond(first["interaction_id"], "select", response, "same-response")
        self.assertEqual(repeated["interaction_id"], result["interaction_id"])
        self.assertEqual(self.store.get_run(first["run_id"])["stage"], "await_approval")

        with self.assertRaises(HilError):
            self.loop.respond(first["interaction_id"], "select", response, "other-response")

    def test_stale_version_and_invalid_selection_are_rejected(self):
        first = self.loop.start()
        with self.assertRaises(HilError):
            self.loop.respond(
                first["interaction_id"],
                "select",
                {"transport_id": "transport_001", "stay_id": "stay_001"},
                expected_state_version=first["state_version"] - 1,
            )

        first = self.loop.start(default_request())
        with self.assertRaises(HilError):
            self.loop.respond(
                first["interaction_id"],
                "select",
                {"transport_id": "not-a-candidate", "stay_id": "stay_001"},
            )

    def test_infeasible_plan_cannot_be_approved(self):
        first = self.loop.start()
        second = self.loop.respond(
            first["interaction_id"],
            "select",
            {"transport_id": "transport_001", "stay_id": "stay_002"},
        )
        self.assertEqual(second["plan"]["status"], "needs_review")
        with self.assertRaises(HilError):
            self.loop.respond(second["interaction_id"], "approve")

    def test_revise_invalidates_old_plan_and_restarts_selection(self):
        first = self.loop.start()
        second = self.loop.respond(
            first["interaction_id"],
            "select",
            {"transport_id": "transport_001", "stay_id": "stay_001"},
        )
        third = self.loop.respond(
            second["interaction_id"],
            "revise",
            {"request_patch": {"budget_cny": 700}},
        )
        self.assertEqual(third["stage"], "await_selection")
        self.assertEqual(third["request_version"], 2)
        self.assertEqual(third["tool_calls"], 4)


if __name__ == "__main__":
    unittest.main()
