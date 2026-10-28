"""Issue #42 acceptance contracts. No expectedFailure, skip or product stand-in.

Every restart crosses a real service process boundary. Model and supplier peers
are synthetic; assertions inspect the actual context sent by Node/Pi.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

import httpx

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).with_name("serve_fixture.py")
DB_NAME = "sessions.sqlite3"
FULL_QUERY = "杭州三亚 2026-11-20"
BUSY = {"running", "cancelling"}


class Service:
    """Owns only the disposable child process tree started by this test."""
    def __init__(self, folder, clock="2026-10-19T10:00:00+08:00"):
        self.folder, self.clock = Path(folder), clock
        self.process = self.client = None

    def start(self):
        endpoint = self.folder / "endpoint.json"
        endpoint.unlink(missing_ok=True)
        self.log = (self.folder / "fixture.log").open("a", encoding="utf-8")
        options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
        self.process = subprocess.Popen([sys.executable, "-B", "-X", "utf8", str(FIXTURE),
            "--root", str(self.folder), "--clock", self.clock], cwd=ROOT,
            stdout=self.log, stderr=self.log, env={**os.environ, "PYTHONUTF8": "1"}, **options)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.log.close()
                return False
            if endpoint.exists():
                try:
                    port = json.loads(endpoint.read_text(encoding="utf-8"))["port"]
                    if self.client is None:
                        self.client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=12, trust_env=False)
                    if self.client.get("/api/session").status_code == 200:
                        return True
                except (httpx.HTTPError, ValueError):
                    pass
            time.sleep(.05)
        self.kill()
        raise AssertionError("Recovery fixture did not become ready; see its temporary fixture.log")

    def kill(self):
        if self.client:
            self.client.close()
            self.client = None
        if self.process and self.process.poll() is None:
            if os.name == "nt":
                # PID comes only from this test's Popen; kill no unrelated server.
                subprocess.run(["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            else:
                os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait(timeout=10)
        if hasattr(self, "log") and not self.log.closed:
            self.log.close()

    def records(self, kind=None):
        path = self.folder / "activity.jsonl"
        # Readers can race a peer's append; observe only completely written lines.
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True) if path.exists() else []
        values = [json.loads(line) for line in lines if line.endswith("\n")]
        return [r for r in values if kind is None or r["kind"] == kind]

    def counts(self):
        return tuple(len(self.records(kind)) for kind in ("model", "supplier"))

    def state(self):
        response = self.client.get("/api/session")
        response.raise_for_status()
        return response.json()

    def send(self, text):
        response = self.client.post("/api/messages", json={"text": text})
        if response.status_code != 202:
            raise AssertionError(f"Expected accepted message, got {response.status_code}: {response.text}")
        return response.json()

    def wait(self, predicate, description, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.04)
        raise AssertionError(f"Timed out waiting for {description}")

    def turn(self, text):
        self.send(text)
        self.wait(lambda: self.state().get("run", {}).get("status") not in BUSY, "turn completion")
        return self.state()

    def hold(self, name):
        (self.folder / ("hold-" + name)).touch()

    def release(self, name):
        (self.folder / ("hold-" + name)).unlink(missing_ok=True)

    def wait_gate(self, name):
        self.wait(lambda: any(r.get("name") == name for r in self.records("gate")), name + " gate")


class RecoveryAcceptance(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="travel-recovery-test-")
        self.addCleanup(self.temp.cleanup)
        self.service = Service(self.temp.name)
        self.addCleanup(self.service.kill)
        self.assertTrue(self.service.start(), "Synthetic fixture must start before testing recovery")

    def restart(self, clock=None):
        self.service.kill()
        if clock:
            self.service.clock = clock
        self.assertTrue(self.service.start(), "Existing valid session must recover")

    def assert_no_calls(self, expected):
        # Requests have actually completed; this is not inferred from UI text.
        self.assertEqual(self.service.counts(), expected)

    def test_fixture_observes_real_model_tool_exchange_without_online_services(self):
        """Harness control: must already pass on main before red tests are trusted."""
        state = self.service.turn(FULL_QUERY)
        self.assertEqual(state["run"]["status"], "complete")
        self.assertEqual(self.service.counts(), (2, 1))
        messages = self.service.records("model")[-1]["messages"]
        self.assertEqual(messages[-1]["role"], "tool")
        self.assertIn("OFFLINE-101", json.dumps(messages, ensure_ascii=False))

    def test_fixture_requires_history_for_partial_query_and_recall(self):
        """Controls prove canned replies cannot mask missing model context."""
        missing = self.service.turn("杭州，2026 年 11 月 20 日。")
        self.assertIn("缺少历史目的地", missing["messages"][-1]["text"])
        self.assert_no_calls((1, 0))
        missing = self.service.turn("刚才查到的航班是什么？")
        self.assertIn("缺少历史工具结果", missing["messages"][-1]["text"])
        self.service.turn("我要去三亚。")
        found = self.service.turn("杭州，2026 年 11 月 20 日。")
        self.assertEqual(found["run"]["status"], "complete")
        self.assert_no_calls((5, 1))
        recalled = self.service.turn("刚才查到的航班是什么？")
        self.assertIn("OFFLINE-101", recalled["messages"][-1]["text"])
        self.assert_no_calls((6, 1))

    def test_fixture_uses_actual_injected_clock_in_relative_query(self):
        state = self.service.turn("查杭州明天去三亚的航班")
        self.assertEqual(state["run"]["status"], "complete")
        self.assertEqual(state["run"]["results"][0]["data"]["departure_date"], "2026-10-20")
        self.assert_no_calls((2, 1))

    def test_A1_partial_conditions_and_original_model_context_survive_restart(self):
        first = self.service.turn("我要去三亚。")
        self.assertEqual(self.service.counts(), (1, 0))
        self.restart()
        self.assertEqual(self.service.state()["session_id"], first["session_id"])
        self.assert_no_calls((1, 0))
        state = self.service.turn("杭州，2026 年 11 月 20 日。")
        self.assertEqual(state["run"]["status"], "complete")
        self.assertEqual(self.service.counts(), (3, 1))
        context = self.service.records("model")[-2]["messages"]
        self.assertIn("我要去三亚。", json.dumps(context, ensure_ascii=False))
        self.assertIn("出发城市", json.dumps(context, ensure_ascii=False))

    def test_A2_completed_facts_and_tool_context_survive_multiple_restarts(self):
        before = self.service.turn(FULL_QUERY)
        self.restart()
        self.assertEqual(self.service.state(), before)
        self.restart()
        self.assertEqual(self.service.state(), before)
        self.assert_no_calls((2, 1))
        recalled = self.service.turn("刚才查到的航班是什么？")
        self.assertIn("OFFLINE-101", recalled["messages"][-1]["text"])
        self.assert_no_calls((3, 1))
        messages = self.service.records("model")[-1]["messages"]
        calls = {call["id"] for m in messages for call in m.get("tool_calls", [])}
        results = [m for m in messages if m["role"] == "tool"]
        self.assertTrue(results, "Restored model must receive tool results, not just display text")
        self.assertTrue(all(m["tool_call_id"] in calls for m in results))

    def test_A2_identifiers_unknown_price_provenance_and_tool_sequence_are_preserved(self):
        before = self.service.turn(FULL_QUERY)
        result = before["run"]["results"][0]
        candidate = result["data"]["candidates"][0]
        self.assertIsNone(candidate["price_cny"])
        self.assertTrue(result["observed_at"])
        self.assertTrue(result["source"])
        self.restart()
        self.assertEqual(self.service.state()["messages"], before["messages"])
        after = self.service.turn(FULL_QUERY)
        second = after["run"]["results"][0]
        self.assertNotEqual(second["call_id"], result["call_id"])
        self.assertNotEqual(second["data"]["candidates"][0]["candidate_id"], candidate["candidate_id"])
        self.assertNotEqual(second["evidence_refs"], result["evidence_refs"])

    def assert_interrupted_then_continue(self, before, expected_counts):
        self.restart()
        restored = self.service.state()
        self.assertEqual(restored["session_id"], before["session_id"])
        self.assertEqual(restored["run"]["id"], before["run"]["id"])
        self.assertEqual(restored["run"]["status"], "interrupted")
        self.assertIn("中断", restored["run"]["stage"])
        self.assertGreater(restored["revision"], before["revision"])
        self.assertEqual(restored["run"]["results"], before["run"]["results"])
        self.assertEqual(restored["messages"][0], before["messages"][0])
        self.assert_no_calls(expected_counts)
        continued = self.service.turn("你能帮我做什么？")
        self.assertEqual(continued["run"]["status"], "complete")
        self.assert_no_calls((expected_counts[0] + 1, expected_counts[1]))
        # A subsequent turn must not erase the interrupted turn's reliable facts.
        if before["run"]["results"]:
            self.assertTrue(any(m.get("results") == before["run"]["results"] for m in continued["messages"]))

    def test_A3_accepted_message_survives_kill_before_first_model_response(self):
        self.service.hold("model")
        self.service.send("我要去三亚。")
        self.service.wait_gate("model")
        before, counts = self.service.state(), self.service.counts()
        self.service.kill()
        self.service.release("model")
        self.assert_interrupted_then_continue(before, counts)
        context = self.service.records("model")[-1]["messages"]
        self.assertIn("我要去三亚。", json.dumps(context, ensure_ascii=False))

    def test_A3_kill_during_supplier_does_not_replay_or_fabricate_result(self):
        self.service.hold("supplier")
        self.service.send(FULL_QUERY)
        self.service.wait_gate("supplier")
        before = self.service.state()
        self.assertEqual(before["run"]["results"], [])
        counts = self.service.counts()
        self.service.kill()
        self.service.release("supplier")
        self.assert_interrupted_then_continue(before, counts)

    def test_A3_visible_tool_result_is_durable_before_final_model_answer(self):
        self.service.hold("after-tool")
        self.service.send(FULL_QUERY)
        self.service.wait_gate("after-tool")
        before = self.service.state()
        self.assertTrue(before["run"]["results"])
        counts = self.service.counts()
        self.service.kill()
        self.service.release("after-tool")
        self.assert_interrupted_then_continue(before, counts)
        recalled = self.service.turn("刚才查到的航班是什么？")
        self.assertIn("OFFLINE-101", recalled["messages"][-1]["text"])

    def test_A4_page_snapshot_and_sse_reconnect_have_no_side_effects(self):
        before = self.service.turn(FULL_QUERY)
        expected = self.service.counts()
        for _ in range(2):
            self.assertEqual(self.service.client.get("/").status_code, 200)
            self.assertEqual(self.service.state(), before)
            with self.service.client.stream("GET", "/api/events") as response:
                self.assertEqual(response.status_code, 200)
                event = next(line[6:] for line in response.iter_lines() if line.startswith("data: "))
                self.assertEqual(json.loads(event), before)
        self.restart()
        self.assertEqual(self.service.state(), before)
        self.assert_no_calls(expected)

    def test_A5_next_day_uses_new_clock_and_keeps_historical_result_unchanged(self):
        before = self.service.turn(FULL_QUERY)
        self.restart(clock="2026-10-20T10:00:00+08:00")
        self.assertEqual(self.service.state()["messages"], before["messages"])
        self.service.turn("刚才查到的航班是什么？")
        self.assert_no_calls((3, 1))
        after = self.service.turn("查杭州明天去三亚的航班")
        self.assertEqual(after["run"]["results"][0]["data"]["departure_date"], "2026-10-21")
        self.assertEqual(after["messages"][:2], before["messages"])
        self.assertIn("2026-10-20T10:00:00+08:00", self.service.records("model")[-2]["messages"][-1]["content"])

    def test_A6_explicit_new_session_is_durable_without_deleting_old_records(self):
        old = self.service.turn(FULL_QUERY)
        database = Path(self.temp.name) / DB_NAME
        self.assertTrue(database.is_file(), "Explicit configured session database must exist")
        response = self.service.client.post("/api/session/reset")
        self.assertEqual(response.status_code, 200)
        new = response.json()
        self.assertNotEqual(new["session_id"], old["session_id"])
        self.assertEqual(new["messages"], [])
        self.restart()
        self.assertEqual(self.service.state(), new)
        self.assert_no_calls((2, 1))
        # Inspect a logical dump instead of pinning storage table/column names.
        with closing(sqlite3.connect(database)) as connection:
            dump = "\n".join(connection.iterdump())
        self.assertIn(old["session_id"], dump)
        self.assertIn("OFFLINE-101", dump)
        self.assertEqual(self.service.turn("你能帮我做什么？")["run"]["status"], "complete")
        new_context = self.service.records("model")[-1]["messages"]
        self.assertNotIn("OFFLINE-101", json.dumps(new_context))

    @contextmanager
    def exclusive_lock(self):
        database = Path(self.temp.name) / DB_NAME
        self.assertTrue(database.is_file(), "Write-failure test requires actual persisted state")
        connection = sqlite3.connect(database, timeout=1)
        # A second writer conflicts under either rollback journal or WAL mode.
        connection.execute("BEGIN EXCLUSIVE")
        try:
            yield
        finally:
            connection.rollback()
            connection.close()

    def test_A7_message_write_failure_is_not_accepted_and_does_not_call_peers(self):
        before = self.service.turn("我要去三亚。")
        counts = self.service.counts()
        with self.exclusive_lock():
            response = self.service.client.post("/api/messages", json={"text": FULL_QUERY})
            self.assertEqual(response.status_code, 503)
            self.assertIn("保存", response.json()["detail"])
        self.assertEqual(self.service.state(), before)
        self.assert_no_calls(counts)
        self.restart()
        self.assertEqual(self.service.state(), before)

    def test_A7_reset_write_failure_keeps_current_session_and_existing_data(self):
        before = self.service.turn(FULL_QUERY)
        with self.exclusive_lock():
            response = self.service.client.post("/api/session/reset")
            self.assertEqual(response.status_code, 503)
        self.assertEqual(self.service.state(), before)
        self.restart()
        self.assertEqual(self.service.state(), before)
        self.assert_no_calls((2, 1))

    def test_A7_tool_checkpoint_write_failure_stops_model_continuation(self):
        self.service.hold("supplier")
        self.service.send(FULL_QUERY)
        self.service.wait_gate("supplier")
        with self.exclusive_lock():
            self.service.release("supplier")
            self.service.wait(lambda: self.service.state()["run"]["status"] not in BUSY,
                              "explicit tool-checkpoint storage failure")
            failed = self.service.state()
            self.assertEqual(failed["run"]["status"], "storage_error")
            self.assertIn("保存", failed["run"]["stage"])
            self.assertEqual(failed["run"]["results"], [], "An unsaved result must not be advertised as durable")
            self.assert_no_calls((1, 1))
        self.restart()
        recovered = self.service.state()
        self.assertIn(recovered["run"]["status"], {"interrupted", "storage_error"})
        self.assertEqual(recovered["run"]["results"], [])
        self.assert_no_calls((1, 1))
        self.assertEqual(self.service.turn("你能帮我做什么？")["run"]["status"], "complete")

    def test_A7_final_checkpoint_write_failure_keeps_prior_durable_tool_result(self):
        self.service.hold("after-tool")
        self.service.send(FULL_QUERY)
        self.service.wait_gate("after-tool")
        before = self.service.state()
        self.assertTrue(before["run"]["results"])
        with self.exclusive_lock():
            self.service.release("after-tool")
            self.service.wait(lambda: self.service.state()["run"]["status"] not in BUSY,
                              "explicit final-checkpoint storage failure")
            failed = self.service.state()
            self.assertEqual(failed["run"]["status"], "storage_error")
            self.assertIn("保存", failed["run"]["stage"])
            self.assertEqual(failed["run"]["results"], before["run"]["results"])
            self.assert_no_calls((2, 1))
        self.restart()
        recovered = self.service.state()
        self.assertIn(recovered["run"]["status"], {"interrupted", "storage_error"})
        self.assertEqual(recovered["run"]["results"], before["run"]["results"])
        recalled = self.service.turn("刚才查到的航班是什么？")
        self.assertIn("OFFLINE-101", recalled["messages"][-1]["text"])
        self.assert_no_calls((3, 1))

    def test_A7_private_model_context_and_credentials_never_enter_public_snapshot(self):
        before = self.service.turn(FULL_QUERY)
        self.restart()
        recovered = self.service.state()
        self.assertEqual(recovered, before, "Privacy cannot pass merely because recovery returned an empty session")
        value = json.dumps(recovered, ensure_ascii=False)
        for private in ("RECOVERY_FIXTURE_PRIVATE_REASONING", "recovery-fixture-synthetic-key",
                        "工具返回事实是唯一依据", '"tool_calls"', '"tool_call_id"'):
            self.assertNotIn(private, value)
        self.assert_no_calls((2, 1))

    def test_A8_cancelled_turn_remains_terminal_after_restart(self):
        self.service.hold("supplier")
        self.service.send(FULL_QUERY)
        self.service.wait_gate("supplier")
        duplicate = self.service.client.post("/api/messages", json={"text": "不应进入会话"})
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(self.service.client.post("/api/session/reset").status_code, 409)
        self.assertEqual(self.service.client.post("/api/cancel").status_code, 200)
        self.service.release("supplier")
        self.service.wait(lambda: self.service.state()["run"]["status"] not in BUSY, "cancelled terminal state")
        before = self.service.state()
        self.assertEqual(before["run"]["status"], "cancelled")
        self.assertEqual(before["run"]["results"], [])
        counts = self.service.counts()
        self.restart()
        self.assertEqual(self.service.state(), before)
        self.assert_no_calls(counts)
        self.assertEqual(self.service.turn("你能帮我做什么？")["run"]["status"], "complete")

    def test_A8_supplier_failure_and_prior_reliable_results_survive_restart(self):
        good = self.service.turn(FULL_QUERY)
        (Path(self.temp.name) / "fail-supplier").touch()
        failed = self.service.turn(FULL_QUERY)
        self.assertEqual(failed["run"]["status"], "supplier_error")
        self.restart()
        self.assertEqual(self.service.state(), failed)
        self.assertEqual(self.service.state()["messages"][:2], good["messages"])
        self.assert_no_calls((4, 2))

    def test_A8_model_failure_and_prior_reliable_results_survive_restart(self):
        good = self.service.turn(FULL_QUERY)
        failed = self.service.turn("模型失败")
        self.assertEqual(failed["run"]["status"], "model_error")
        counts = self.service.counts()
        self.restart()
        self.assertEqual(self.service.state(), failed)
        self.assertEqual(self.service.state()["messages"][:2], good["messages"])
        self.assert_no_calls(counts)


class InvalidStorageAcceptance(unittest.TestCase):
    def assert_refuses_without_overwriting(self, prepare):
        with tempfile.TemporaryDirectory(prefix="travel-recovery-invalid-") as folder:
            database = Path(folder) / DB_NAME
            prepare(database)
            before = database.read_bytes() if database.is_file() else None
            service = Service(folder)
            try:
                self.assertFalse(service.start(), "Invalid durable storage must fail visibly, not start an empty session")
                self.assertEqual(service.counts(), (0, 0))
                log = (Path(folder) / "fixture.log").read_text(encoding="utf-8")
                self.assertTrue(log.strip(), "Startup failure must be diagnosable")
                if before is not None:
                    self.assertEqual(database.read_bytes(), before)
            finally:
                service.kill()

    def test_A7_corrupt_database_is_not_silently_replaced(self):
        self.assert_refuses_without_overwriting(lambda p: p.write_bytes(b"synthetic not-a-sqlite-database"))

    def test_A7_newer_schema_version_is_not_migrated_or_overwritten(self):
        def prepare(path):
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("PRAGMA user_version = 999999")
                connection.execute("CREATE TABLE future_private_state(value TEXT)")
                connection.execute("INSERT INTO future_private_state VALUES ('synthetic-preserve-me')")
        self.assert_refuses_without_overwriting(prepare)

    def test_A7_unwritable_database_target_is_not_ignored(self):
        self.assert_refuses_without_overwriting(lambda p: p.mkdir())


if __name__ == "__main__":
    unittest.main()
