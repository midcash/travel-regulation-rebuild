"""Issue #44 contracts. Every test owns a fresh temporary SQLite store; no live peers."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from control import HERE, Ledger, Node

PLAN = {"selected": {"flight": "chosen-A"}, "recommendations": [{"flight": "suggested-B"}],
        "history": [{"flight": "OFFLINE-101", "price": None, "queriedAt": "2026-10-29T10:00:00+08:00"}]}


class DurableContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="durable-step0-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ledger = Ledger(self.root)
        self.assertTrue((HERE / "worker.mjs").is_file(), "Pi Durable probe worker is not implemented")

    def node(self, **options):
        node = Node(self.root, self.ledger, **options)
        self.addCleanup(node.kill)
        return node

    def assertCounts(self, model, tool):
        self.assertEqual(self.ledger.counts(), (model, tool))

    def test_v1_restore_and_request_id_exactly_once(self):
        node = self.node()
        done = node.call("prompt", text="hello", requestId="same-request")
        before = node.call("snapshot")
        self.assertEqual(done["status"], "done")
        self.assertCounts(1, 0)
        node.kill()
        restored = self.node()
        self.assertEqual(restored.call("snapshot")["entries"], before["entries"])
        self.assertEqual(restored.call("snapshot")["conversationId"], before["conversationId"])
        self.assertEqual(restored.call("prompt", text="hello", requestId="same-request"), done)
        self.assertCounts(1, 0)

    def test_v2_safe_python_bridge_and_interrupted_model(self):
        node = self.node()
        node.call("prompt", text="completed", requestId="completed", scenario="safe")
        self.assertCounts(2, 1)
        node.hold("tool:interrupted")
        node.begin("prompt", text="interrupted", requestId="interrupted", scenario="safe")
        node.until(lambda: self.ledger.counts() == (3, 2))
        self.assertTrue(any(t.is_alive() for t in node.threads))
        node.kill()
        restored = self.node()
        restored.call("resume")
        restored.call("idle")
        self.assertCounts(4, 3)
        calls = self.ledger.read("tool")
        self.assertEqual([c["args"]["tag"] for c in calls], ["completed", "interrupted", "interrupted"])
        self.assertEqual([c["name"] for c in calls], ["search_flights"] * 3)
        restored.hold("model:model-cut")
        restored.begin("prompt", text="model-cut", requestId="model-cut")
        restored.until(lambda: self.ledger.counts() == (5, 3))
        restored.kill()
        again = self.node()
        again.call("resume")
        again.call("idle")
        self.assertCounts(6, 3)

    def test_v2_unsafe_tool_is_reported_without_reexecution(self):
        node = self.node()
        node.hold("tool:unsafe-cut")
        node.begin("prompt", text="unsafe-cut", requestId="unsafe-cut", scenario="unsafe")
        node.until(lambda: self.ledger.counts() == (1, 1))
        node.kill()
        restored = self.node()
        restored.call("resume")
        restored.call("idle")
        self.assertCounts(2, 1)
        request = self.ledger.read("model")[-1]["request"]
        self.assertIn("interrupted", json.dumps(request).lower())
        self.assertEqual(self.ledger.read("tool")[0]["name"], "unsafe_effect")

    def test_v3_document_survives_and_is_in_actual_model_request(self):
        node = self.node()
        node.call("prompt", text="plan", requestId="plan", scenario="plan")
        self.assertEqual(node.call("snapshot")["plan"], PLAN)
        node.kill()
        restored = self.node()
        self.assertEqual(restored.call("snapshot")["plan"], PLAN)
        restored.call("prompt", text="recall", requestId="recall")
        request = json.dumps(self.ledger.read("model")[-1]["request"], ensure_ascii=False)
        systems = [m for m in self.ledger.read("model")[-1]["request"]["messages"] if m["role"] == "system"]
        plan_section = next(m["sections"]["trip-plan"] for m in systems if "trip-plan" in m.get("sections", {}))
        for value in ("chosen-A", "suggested-B", "OFFLINE-101", "2026-10-29T10:00:00+08:00"):
            self.assertIn(value, request)
            self.assertIn(value, plan_section)
        self.assertCounts(3, 1)

    def test_v3_document_and_transcript_are_atomic_before_and_after_commit(self):
        for boundary, expected in (("before", False), ("after", True)):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory(prefix="durable-atomic-") as folder:
                ledger = Ledger(Path(folder))
                node = Node(Path(folder), ledger, commitGate=boundary)
                self.addCleanup(node.kill)
                node.hold("commit:" + boundary)
                node.begin("prompt", text="plan", requestId="plan", scenario="plan")
                node.until(lambda: any(e.get("gate") == "commit:" + boundary for e in node.events))
                node.kill()
                restored = Node(Path(folder), ledger)
                try:
                    state = restored.call("snapshot")  # no resume: inspect the exact recovered boundary
                    self.assertEqual(state["plan"] == PLAN, expected)
                    self.assertEqual(sum(e["kind"] == "probe.plan-change" for e in state["entries"]), int(expected))
                    self.assertEqual(ledger.counts(), (1, 1))
                finally:
                    restored.kill()

    def timer_case(self, late):
        node = self.node(clockMs=1000000)
        until = 1001000
        task = node.call("timer", until=until)
        node.call("idle")
        node.call("abort")
        self.assertEqual(node.call("task", taskId=task)["state"]["status"], "sleeping")
        node.kill()
        started = until + 100 if late else until - 500
        restored = self.node(clockMs=started)
        self.assertEqual(started > until, late)
        restored.call("resume")
        if not late:
            restored.call("idle")
            restored.call("clock", now=until - 1)
            self.assertEqual(len(self.ledger.read("timer")), 0)
            self.assertEqual(restored.call("task", taskId=task)["state"]["status"], "sleeping")
            restored.call("clock", now=until)
        outcome = restored.call("waitTask", taskId=task)
        self.assertEqual(outcome["state"]["outcome"]["status"], "completed")
        calls = self.ledger.read("timer")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["at"], max(until, started))
        restored.kill()
        again = self.node(clockMs=until + 200)
        again.call("resume")
        again.call("idle")
        self.assertEqual(len(self.ledger.read("timer")), 1)
        self.assertCounts(0, 0)

    def test_v4_restart_before_deadline(self):
        self.timer_case(False)

    def test_v4_restart_after_deadline(self):
        self.timer_case(True)

    def test_v5_network_is_denied_and_versions_are_exact(self):
        node = self.node()
        self.assertEqual(node.call("networkAudit"), {"attemptsBlocked": 3})
        self.assertEqual(len(self.ledger.read("network")), 3)
        self.assertCounts(0, 0)
        package = json.loads((HERE / "package.json").read_text(encoding="utf-8"))
        for name in ("pi-durable", "pi-ai", "chord"):
            self.assertEqual(package["dependencies"]["@earendil-works/" + name], "1.4.0")

    def test_v6_watch_only_committed_and_sequence_survives_restart(self):
        node = self.node(commitGate="before")
        node.call("watch")
        node.hold("commit:before")
        pending = node.begin("prompt", text="plan", requestId="plan", scenario="plan")
        node.until(lambda: any(e.get("gate") == "commit:before" for e in node.events))
        observed = node.call("views")
        self.assertFalse(any(e["kind"] == "probe.plan-change" for v in observed["views"] for e in v["entries"]))
        node.release("commit:before")
        node.result(pending)
        node.call("idle")
        state = node.call("snapshot")
        batches = [p for p in state["publications"] if any(c["type"] == "entry" and
                   c["value"]["kind"] == "probe.plan-change" for c in p["changes"])]
        self.assertEqual(len(batches), 1)
        self.assertEqual(sum(c["type"] == "document" and c["record"]["kind"] == "probe.trip-plan"
                             for c in batches[0]["changes"]), 1)
        self.assertTrue(any(e["kind"] == "probe.plan-change" for v in state["views"] for e in v["entries"]))
        self.assertTrue(all(b > a for a, b in zip(state["sequences"], state["sequences"][1:])))
        last = state["sequences"][-1]
        node.kill()
        restored = self.node()
        self.assertEqual(restored.call("snapshot")["entries"], state["entries"])
        restored.call("prompt", text="next", requestId="next")
        self.assertGreater(restored.call("snapshot")["sequences"][0], last)
        self.assertCounts(3, 1)

    def test_observe_abort_bridge_and_invalid_storage(self):
        node = self.node()
        node.hold("tool:cancel")
        node.begin("prompt", text="cancel", requestId="cancel", scenario="safe")
        node.until(lambda: self.ledger.counts() == (1, 1))
        abort = node.begin("abort")
        node.until(lambda: any(e.get("kind") == "bridge-abort" for e in node.events))
        self.assertTrue(any(t.is_alive() for t in node.threads))
        node.release("tool:cancel")
        node.result(abort)
        self.assertCounts(1, 1)
        node.kill()
        for mode in ("corrupt", "future-schema"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory(prefix="durable-invalid-") as folder:
                broken = Node(Path(folder), Ledger(Path(folder)), invalid=mode)
                try:
                    result = broken.call("invalidStorage")
                    self.assertEqual(result["rejected"], True)
                    self.assertTrue(result["error"])
                    path = Path(folder) / "session.sqlite"
                    if mode == "corrupt":
                        self.assertEqual(path.read_bytes(), b"not a SQLite file")
                    else:
                        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
                            self.assertEqual(db.execute("SELECT version FROM durable_schema").fetchone(), (999999,))
                            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                            self.assertEqual(tables, {"durable_schema"})
                finally:
                    broken.kill()

    def test_v1_conflicting_request_id_reuses_original_and_interrupted_retry_deduplicates(self):
        node = self.node()
        first = node.call("prompt", text="first", requestId="conflict")
        self.assertEqual(node.call("prompt", text="different body", requestId="conflict"), first)
        self.assertCounts(1, 0)
        node.hold("model:cut")
        node.begin("prompt", text="cut", requestId="cut")
        node.until(lambda: self.ledger.counts() == (2, 0))
        node.kill()
        restored = self.node()
        restored.call("resume")
        self.assertEqual(restored.call("prompt", text="cut", requestId="cut")["status"], "done")
        self.assertCounts(3, 0)

    def test_v2_completed_tool_in_same_round_is_not_replayed(self):
        node = self.node()
        node.hold("tool:chain-cut")
        node.begin("prompt", text="chain", requestId="chain", scenario="multi")
        node.until(lambda: self.ledger.counts() == (1, 2))
        node.kill()
        restored = self.node()
        restored.call("resume")
        restored.call("idle")
        self.assertCounts(2, 3)
        self.assertEqual([r["args"]["tag"] for r in self.ledger.read("tool")],
                         ["chain-completed", "chain-cut", "chain-cut"])

    def test_v3_kill_inside_sql_transaction_rolls_back_both_writes(self):
        node = self.node(commitGate="inside")
        node.hold("commit:inside")
        node.begin("prompt", text="plan", requestId="plan", scenario="plan")
        node.until(lambda: any(e.get("gate") == "commit:inside" for e in node.events))
        node.kill()
        restored = self.node()
        state = restored.call("snapshot")
        self.assertIsNone(state["plan"])
        self.assertEqual(sum(e["kind"] == "probe.plan-change" for e in state["entries"]), 0)
        self.assertCounts(1, 1)

    def test_v4_explicit_background_abort_cancels_timer(self):
        node = self.node()
        task = node.call("timer", until=int(time.time() * 1000) + 60000)
        node.call("abort", background=True)
        self.assertEqual(node.call("task", taskId=task)["state"]["outcome"]["status"], "aborted")
        node.kill()
        restored = self.node()
        restored.call("resume")
        restored.call("idle")
        self.assertEqual(restored.call("task", taskId=task)["state"]["outcome"]["status"], "aborted")
        self.assertEqual(len(self.ledger.read("timer")), 0)
        self.assertCounts(0, 0)

    def test_v4_past_deadline_returns_completed_task(self):
        node = self.node(clockMs=2000)
        task = node.result(node.begin("timer", until=1000), timeout=5)
        settled = node.call("waitTask", taskId=task)
        self.assertEqual(settled["state"]["outcome"]["status"], "completed")
        self.assertEqual([r["at"] for r in self.ledger.read("timer")], [2000])
        self.assertCounts(0, 0)

    def test_v6_storage_failure_does_not_publish_uncommitted_plan(self):
        node = self.node(failCommit=True)
        node.call("watch")
        with self.assertRaises(AssertionError):
            node.call("prompt", text="plan", requestId="plan", scenario="plan")
        node.until(lambda: any(e.get("info", {}).get("type") == "session_failed" for e in node.events))
        self.assertFalse(any(e["kind"] == "probe.plan-change"
                             for v in node.call("views")["views"] for e in v["entries"]))
        node.kill()
        restored = self.node()
        self.assertIsNone(restored.call("snapshot")["plan"])
        self.assertEqual(sum(e["kind"] == "probe.plan-change" for e in restored.call("snapshot")["entries"]), 0)
        self.assertCounts(1, 1)
