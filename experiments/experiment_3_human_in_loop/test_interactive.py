import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from .runtime import HilError, HilStore, TravelHilLoop


class InteractiveRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.db = Path(self.temp.name) / "check.sqlite3"
        self.store = HilStore(self.db)
        self.loop = TravelHilLoop(self.store)

    def tearDown(self):
        self.temp.cleanup()

    def test_expiry_is_committed_and_cannot_be_consumed(self):
        first = self.loop.start()
        with self.store.connect() as db:
            db.execute("UPDATE human_interactions SET expires_at=? WHERE interaction_id=?",
                       ("2000-01-01T00:00:00+00:00", first["interaction_id"]))
        result = self.loop.respond(first["interaction_id"], "cancel")
        self.assertEqual(result["status"], "expired")
        fresh = HilStore(self.db)
        self.assertEqual(fresh.get_run(first["run_id"])["status"], "expired")
        self.assertEqual(fresh.get_interaction(first["interaction_id"])["status"], "expired")
        self.assertEqual(sum(x["type"] == "human_expired" for x in fresh.list_events(first["run_id"])), 1)

    def test_crash_rolls_back_reply_and_can_retry(self):
        first = self.loop.start()
        payload = {"transport_id": "transport_001", "stay_id": "stay_001"}
        with patch.object(self.loop, "_advance", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self.loop.respond(first["interaction_id"], "select", payload, "retry-1")
        fresh = TravelHilLoop(HilStore(self.db))
        self.assertEqual(fresh.store.get_interaction(first["interaction_id"])["status"], "pending")
        result = fresh.respond(first["interaction_id"], "select", payload, "retry-1")
        self.assertEqual(result["stage"], "await_approval")
        self.assertEqual(result["plan"]["total_cny"], 628)

    def test_legacy_consumed_checkpoint_recovers_once(self):
        first = self.loop.start()
        response = {"action": "select", "payload": {"transport_id": "transport_001", "stay_id": "stay_001"}}
        self.store.consume_interaction(first["interaction_id"], "legacy", first["state_version"], response)
        fresh = TravelHilLoop(HilStore(self.db))
        result = fresh.resume(first["run_id"])
        again = fresh.resume(first["run_id"])
        self.assertEqual(result["stage"], "await_approval")
        self.assertEqual(result["interaction_id"], again["interaction_id"])
        self.assertEqual(again["tool_calls"], 3)

    def test_next_request_and_checkpoint_roll_back_together(self):
        first = self.loop.start()
        with patch.object(self.store, "save_run", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self.loop.respond(first["interaction_id"], "select",
                                  {"transport_id": "transport_001", "stay_id": "stay_001"})
        with self.store.connect() as db:
            count = db.execute("SELECT COUNT(*) FROM human_interactions WHERE run_id=?", (first["run_id"],)).fetchone()[0]
        self.assertEqual(count, 1)
        self.assertEqual(self.store.get_interaction(first["interaction_id"])["status"], "pending")

    def test_changed_reply_with_same_id_is_rejected(self):
        first = self.loop.start()
        self.loop.respond(first["interaction_id"], "cancel", response_id="same")
        with self.assertRaises(HilError):
            self.loop.respond(first["interaction_id"], "select", response_id="same")

    def run_cli(self, inputs, *args):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        return subprocess.run([sys.executable, "-X", "utf8", "-B",
                               str(Path(__file__).with_name("cli.py").resolve()),
                               "--db", str(self.db.resolve()), *args],
                              input=inputs, text=True, encoding="utf-8", capture_output=True,
                              cwd=self.temp.name, env=env, timeout=15)

    def test_direct_script_full_chinese_interaction(self):
        result = self.run_cli("bad\n1\n1\n1\n1\n1\n0\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("请输入 1", result.stdout)
        self.assertIn("628 元", result.stdout)
        self.assertIn("方案已确认，任务完成", result.stdout)
        self.assertEqual(self.store.list_runs()[0]["status"], "completed")

    def test_exit_then_continue_in_another_process(self):
        first = self.run_cli("1\n0\n0\n")
        self.assertEqual(first.returncode, 0, first.stderr)
        run_id = self.store.list_runs()[0]["run_id"]
        interaction_id = self.store.get_run(run_id)["last_interaction_id"]
        second = self.run_cli("2\n1\n1\n1\n1\n1\n0\n")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.store.get_run(run_id)["status"], "completed")
        self.assertEqual(self.store.get_interaction(interaction_id)["status"], "consumed")
        self.assertEqual(len(self.store.list_runs()), 1)

    def test_infeasible_choice_can_be_reselected(self):
        result = self.run_cli("1\n1\n1\n2\n1\n3\n1\n1\n1\n1\n0\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("当前方案未通过校验", result.stdout)
        self.assertIn("方案已确认，任务完成", result.stdout)
        state = self.store.get_run(self.store.list_runs()[0]["run_id"])
        self.assertEqual(state["request_version"], 2)

    def test_old_command_mode_remains_json(self):
        result = self.run_cli("", "start")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["stage"], "await_selection")


if __name__ == "__main__":
    unittest.main()
