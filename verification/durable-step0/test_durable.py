"""Issue #44 contracts. Every test owns a fresh temporary SQLite store; no live peers."""
import json
from pathlib import Path
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
        for value in ("chosen-A", "suggested-B", "OFFLINE-101", "2026-10-29T10:00:00+08:00"):
            self.assertIn(value, request)
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
        node = self.node()
        until = int(time.time() * 1000) + 6000
        task = node.call("timer", until=until)
        node.call("idle")
        node.call("abort")
        self.assertEqual(node.call("task", taskId=task)["state"]["status"], "sleeping")
        node.kill()
        if late:
            time.sleep(max(0, until / 1000 - time.time()) + .1)
        restored = self.node()
        started = time.time() * 1000
        self.assertEqual(started > until, late)
        restored.call("resume")
        outcome = restored.call("waitTask", taskId=task)
        self.assertEqual(outcome["state"]["outcome"]["status"], "completed")
        calls = self.ledger.read("timer")
        self.assertEqual(len(calls), 1)
        self.assertTrue(until <= calls[0]["at"] <= max(until, started) + 3000)
        restored.kill()
        again = self.node()
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
                    self.assertEqual(result["emptySessionCreated"], False)
                finally:
                    broken.kill()
