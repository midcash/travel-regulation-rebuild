"""Portable Pi protocol checks: no private replay fixtures or paid models."""
from pathlib import Path
import tempfile
import unittest

from control import Journal, Node, summary, tool_schema


class PortableContracts(unittest.TestCase):
    def test_offline_process_stream_restore_and_isolation(self):
        with tempfile.TemporaryDirectory() as folder:
            journal = Journal(Path(folder) / "journal.sqlite3")
            node = Node()
            try:
                node.call("init", fake=True, tools=[tool_schema()])
                out = node.call("prompt", text="查询三亚", responses=[{"tool": "lookup", "args": {"city": "三亚"}},
                    {"text": "三亚，证据 sanya-1。"}], tool=lambda event: journal.tool(event,
                        lambda _: {"status": "ok", "city": "三亚", "evidence_ids": ["sanya-1"]}))
                self.assertEqual((out["modelCalls"], out["toolRequests"]), (2, 1))
                self.assertTrue(any(e["type"] == "text_delta" for e in node.events))
                node.call("prompt", text="历史，仅测试，不改变选择。" * 100, responses=[{"text": "已记录。"}])
                node.call("cancel")
                compacted = node.call("compact")
                self.assertTrue(any(e["type"] == "compaction" for e in compacted["entries"]))
                injected = node.call("inject", text="已选酒店 B，证据 evidence-hotel-B，未预订。", version=2)
                journal.save(injected["entries"])
                self.assertEqual(node.call("snapshot")["modelCalls"], injected["modelCalls"])
                audit = node.call("audit")
                self.assertEqual(audit["negativeRejected"], 2)
                self.assertIsNone(audit.get("sessionFile"))
                self.assertFalse(any(p["path"].endswith("agents.md") or ".agents" in Path(p["path"]).parts
                    for p in audit["accesses"]))
                self.assertEqual(list(Path(node.temp.name).rglob("*.jsonl")), [])
                next_turn = node.call("prompt", text="继续", responses=[{"text": "继续处理。"}])
                self.assertEqual(next_turn["modelCalls"], injected["modelCalls"] + 1)
            finally:
                node.close()
            restored = Node()
            try:
                restored.call("init", fake=True, entries=journal.load())
                snapshot = restored.call("snapshot")
                self.assertEqual(snapshot["modelCalls"], 0)
                self.assertTrue(any(m["role"] == "compactionSummary" for m in snapshot["messages"]))
                self.assertIn("evidence-hotel-B", str(snapshot["messages"][-1]))
            finally:
                restored.close()

    def test_cancel_and_kill_during_tool_wait_do_not_start_new_work(self):
        for kill in (False, True):
            with self.subTest(kill=kill), tempfile.TemporaryDirectory() as folder:
                journal = Journal(Path(folder) / "journal.sqlite3")
                node = Node()
                try:
                    node.call("init", fake=True, tools=[tool_schema()])
                    node.send("prompt", text="查询三亚", responses=[{"tool": "lookup", "args": {"city": "三亚"}}])
                    while True:
                        event = node.receive(5)
                        if event["type"] == "exit":
                            self.fail("Worker exited before tool request")
                        if event["type"] == "tool_request":
                            break
                    with self.assertRaises(InterruptedError):
                        journal.tool(event, lambda _: {"status": "ok"}, interrupt=True)
                    if not kill:
                        node.call("cancel")
                        snapshot = node.call("snapshot")
                        self.assertEqual(snapshot["modelCalls"], 1)
                        self.assertEqual(sum(e["type"] == "tool_request" for e in node.events), 1)
                finally:
                    node.close(kill=kill)
                self.assertIsNotNone(node.process.poll())
                with journal.connect() as db:
                    self.assertEqual(db.execute("SELECT status FROM pi0_tools").fetchone()[0], "unknown")

    def test_cancelled_provider_error_is_not_complete_usage(self):
        result = summary([{"type": "model_start"}, {"type": "model_end", "stopReason": "error",
            "error": "This operation was aborted", "usage": {"totalTokens": 0}}])
        self.assertFalse(result["usage_complete"])

    def test_missing_model_end_is_not_complete_usage(self):
        result = summary([{"type": "model_start"}, {"type": "model_end", "stopReason": "stop", "usage": {}},
            {"type": "model_start"}])
        self.assertFalse(result["usage_complete"])

    def test_stream_exception_finishes_prompt_and_shutdown(self):
        node = Node()
        try:
            node.call("init", fake=True, failStream=True)
            result = node.wait(node.send("prompt", text="test", responses=[{"text": "unused"}]), timeout=3)
            self.assertTrue(result["entries"])
            self.assertFalse(summary(node.events)["usage_complete"])
            self.assertTrue(summary(node.events)["errors"])
        finally:
            node.close()

    def test_unknown_requires_new_id_and_completed_result_is_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            journal = Journal(Path(folder) / "journal.sqlite3")
            calls = []
            def supplier(event):
                calls.append(event["id"])
                return {"status": "ok"}
            event = {"id": "a", "args": {"city": "三亚"}}
            first = journal.tool(event, supplier)
            self.assertEqual(first, journal.tool(event, supplier))
            self.assertEqual(calls, ["a"])
            with self.assertRaisesRegex(ValueError, "Conflicting duplicate"):
                journal.tool({"id": "a", "args": {"city": "海口"}}, supplier)
            with self.assertRaises(InterruptedError):
                journal.tool({"id": "b", "args": {}}, supplier, interrupt=True)
            with self.assertRaisesRegex(ValueError, "Unknown outcome"):
                journal.tool({"id": "b", "args": {}}, supplier)
            journal.tool({"id": "c", "args": {}}, supplier)
            self.assertEqual(calls, ["a", "b", "c"])

    def test_actual_query_and_captured_version_survive_later_selection(self):
        with tempfile.TemporaryDirectory() as folder:
            journal = Journal(Path(folder) / "journal.sqlite3")
            result = journal.tool({"id": "a", "args": {"city": "三亚"}}, lambda _: {"status": "ok"}, version=1)
            journal.select("hotel", 2, {"candidate": "B"})
            self.assertEqual(result["condition_version"], 1)
            self.assertEqual(result["query"], {"city": "三亚"})
            with self.assertRaisesRegex(ValueError, "Stale"):
                journal.select("hotel", 1, {"candidate": "A"})
