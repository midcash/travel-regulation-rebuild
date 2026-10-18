"""White-box checks for the SQLite conversation store (temporary databases only)."""
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from travel_agent.conversations import (
    SHANGHAI, ConversationBusy, ConversationStore, MessageConflict, NotFound, RunNotActive,
    SchemaMismatch, ToolCallConflict, ToolOutcomeUnknown)


class Clock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, **delta):
        self.value += timedelta(**delta)


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "conversations.sqlite3"
        self.clock = Clock(datetime(2099, 3, 1, 9, 0, tzinfo=SHANGHAI))
        self.store = ConversationStore(self.path, clock=self.clock)

    def start(self, text="上海到三亚 2099-03-05 的航班", message_id="m1"):
        conversation = self.store.create_conversation("测试")
        submitted = self.store.submit_message(conversation["id"], message_id, text)
        return conversation["id"], submitted["run"]["id"]

    def call_status(self, run_id, call_id):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute("SELECT status FROM pi_tool_calls WHERE run_id=? AND call_id=?",
                              (run_id, call_id)).fetchone()[0]

    def event_types(self, conversation_id):
        return [e["type"] for e in self.store.events_after(conversation_id)]


class Messages(StoreCase):
    def test_submit_records_shanghai_time_and_starts_one_run(self):
        conversation_id, run_id = self.start()
        state = self.store.get_state(conversation_id)
        [message] = state["messages"]
        self.assertEqual(message["captured_at"], "2099-03-01T09:00:00.000000+08:00")
        self.assertEqual(message["run_id"], run_id)
        self.assertEqual(state["active_run"]["id"], run_id)
        self.assertEqual(state["active_run"]["status"], "running")
        self.assertEqual(self.event_types(conversation_id),
                         ["conversation_created", "message", "run_started"])
        self.assertEqual(state["last_event_seq"], 3)

    def test_same_message_id_and_text_is_idempotent(self):
        conversation_id, run_id = self.start()
        again = self.store.submit_message(conversation_id, "m1", "上海到三亚 2099-03-05 的航班")
        self.assertFalse(again["created"])
        self.assertEqual(again["run"]["id"], run_id)
        self.assertEqual(len(self.store.get_state(conversation_id)["messages"]), 1)
        self.assertEqual(len(self.store.events_after(conversation_id)), 3)

    def test_same_message_id_with_other_text_or_conversation_is_rejected(self):
        conversation_id, _ = self.start()
        with self.assertRaises(MessageConflict):
            self.store.submit_message(conversation_id, "m1", "改成北京")
        other = self.store.create_conversation()["id"]
        with self.assertRaises(MessageConflict):
            self.store.submit_message(other, "m1", "上海到三亚 2099-03-05 的航班")
        self.assertEqual(self.store.get_state(other)["messages"], [])

    def test_second_message_waits_for_active_run(self):
        conversation_id, run_id = self.start()
        with self.assertRaises(ConversationBusy):
            self.store.submit_message(conversation_id, "m2", "再查一次")
        self.store.finish_run(run_id, "completed", usage={"input": 10, "output": 5})
        second = self.store.submit_message(conversation_id, "m2", "再查一次")
        self.assertTrue(second["created"])
        self.assertEqual(second["message"]["seq"], 2)

    def test_concurrent_submits_start_only_one_run(self):
        conversation_id = self.store.create_conversation()["id"]
        outcomes, barrier = [], threading.Barrier(4)

        def submit(index):
            barrier.wait()
            try:
                ConversationStore(self.path, clock=self.clock).submit_message(
                    conversation_id, f"m{index}", f"消息 {index}")
                outcomes.append("ok")
            except ConversationBusy:
                outcomes.append("busy")

        threads = [threading.Thread(target=submit, args=(i,)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(outcomes), ["busy", "busy", "busy", "ok"])

    def test_invalid_input_and_unknown_conversation_write_nothing(self):
        with self.assertRaises(NotFound):
            self.store.submit_message("conv_missing", "m1", "你好")
        conversation_id = self.store.create_conversation()["id"]
        for message_id, text in (("", "你好"), ("x" * 129, "你好"), ("m1", "   ")):
            with self.assertRaises(ValueError):
                self.store.submit_message(conversation_id, message_id, text)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM pi_messages").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM pi_runs").fetchone()[0], 0)

    def test_naive_clock_is_rejected(self):
        store = ConversationStore(self.path, clock=lambda: datetime(2099, 3, 1, 9, 0))
        with self.assertRaises(ValueError):
            store.create_conversation()


class Dates(StoreCase):
    def test_each_message_and_today_reread_the_clock_across_midnight(self):
        self.clock.value = datetime(2099, 3, 1, 23, 59, 59, tzinfo=SHANGHAI)
        conversation_id, run_id = self.start(message_id="before")
        self.assertEqual(str(self.store.today()), "2099-03-01")
        self.store.finish_run(run_id, "completed")
        self.clock.advance(seconds=2)
        self.assertEqual(str(self.store.today()), "2099-03-02")
        self.store.submit_message(conversation_id, "after", "明天的航班")
        first, second = self.store.get_state(conversation_id)["messages"]
        self.assertTrue(first["captured_at"].startswith("2099-03-01T23:59:59"))
        self.assertTrue(second["captured_at"].startswith("2099-03-02T00:00:01"))

    def test_utc_clock_is_converted_to_shanghai(self):
        from datetime import timezone
        self.clock.value = datetime(2099, 3, 1, 16, 30, tzinfo=timezone.utc)
        self.assertEqual(str(self.store.today()), "2099-03-02")


class Tools(StoreCase):
    def test_result_and_evidence_commit_together_and_ids_are_idempotent(self):
        conversation_id, run_id = self.start()
        args = {"origin": "上海", "destination": "三亚", "date": "2099-03-05"}
        self.assertIsNone(self.store.begin_tool_call(run_id, "call1", "search_transport", args))
        with self.assertRaises(ToolOutcomeUnknown):
            self.store.begin_tool_call(run_id, "call1", "search_transport", args)
        stored = self.store.complete_tool_call(
            run_id, "call1", {"status": "ok", "count": 1},
            [{"facts": {"flight": "MU1234"}, "raw": {"secret_field": "x"}, "fetched_at": "2099-03-01T09:00:01+08:00"}])
        [evidence_id] = stored["evidence_ids"]
        self.assertEqual(self.store.begin_tool_call(run_id, "call1", "search_transport", args), stored)
        with self.assertRaises(ToolCallConflict):
            self.store.begin_tool_call(run_id, "call1", "search_transport", {**args, "date": "2099-03-06"})
        evidence = self.store.get_evidence(conversation_id, evidence_id)
        self.assertEqual(evidence["facts"], {"flight": "MU1234"})
        self.assertEqual((evidence["run_id"], evidence["call_id"]), (run_id, "call1"))
        self.assertNotIn("raw", evidence)
        self.assertEqual(self.store.get_evidence(conversation_id, evidence_id, include_raw=True)["raw"],
                         {"secret_field": "x"})
        self.assertEqual(self.event_types(conversation_id)[-2:], ["tool_started", "tool_result"])
        self.assertNotIn("secret_field", str(self.store.events_after(conversation_id)))

    def test_failed_commit_leaves_no_partial_evidence(self):
        conversation_id, run_id = self.start()
        self.store.begin_tool_call(run_id, "call1", "search_transport", {"date": "2099-03-05"})
        with self.assertRaises(TypeError):
            self.store.complete_tool_call(run_id, "call1", {"status": "ok"},
                                          [{"facts": {"ok": 1}}, {"facts": {"bad": object()}}])
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM pi_evidence").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT status FROM pi_tool_calls").fetchone()[0], "pending")

    def test_supplier_failure_is_kept_as_a_known_result(self):
        conversation_id, run_id = self.start()
        self.store.begin_tool_call(run_id, "call1", "search_transport", {"date": "2099-03-05"})
        failed = self.store.fail_tool_call(run_id, "call1", "supplier_unavailable", "HTTP 503")
        self.assertEqual(failed["error"]["code"], "supplier_unavailable")
        self.assertEqual(self.store.begin_tool_call(run_id, "call1", "search_transport",
                                                    {"date": "2099-03-05"}), failed)

    def test_evidence_is_not_visible_from_another_conversation(self):
        conversation_id, run_id = self.start()
        self.store.begin_tool_call(run_id, "call1", "search_transport", {})
        [evidence_id] = self.store.complete_tool_call(run_id, "call1", {}, [{"facts": {"a": 1}}])["evidence_ids"]
        other_id, other_run = self.start(message_id="m-other")
        with self.assertRaises(NotFound):
            self.store.get_evidence(other_id, evidence_id)
        # Call IDs are scoped to their run: the same ID elsewhere is a fresh call, not a conflict.
        self.assertIsNone(self.store.begin_tool_call(other_run, "call1", "search_transport", {}))
        self.assertNotIn("tool_result", self.event_types(other_id))


class Cancellation(StoreCase):
    def test_cancel_blocks_new_work_and_late_results_are_archived_only(self):
        conversation_id, run_id = self.start()
        self.store.begin_tool_call(run_id, "call1", "search_transport", {})
        self.assertEqual(self.store.request_cancel(run_id), "cancelling")
        self.assertEqual(self.store.request_cancel(run_id), "cancelling")
        with self.assertRaises(RunNotActive):
            self.store.begin_tool_call(run_id, "call2", "search_transport", {})
        with self.assertRaises(RunNotActive):
            self.store.add_assistant_message(run_id, "迟到的回答")
        late = self.store.complete_tool_call(run_id, "call1", {"status": "ok"}, [{"facts": {"late": 1}}])
        self.assertIsNone(late)
        self.assertEqual(self.store.finish_run(run_id, "completed"), "cancelled")
        state = self.store.get_state(conversation_id)
        self.assertIsNone(state["active_run"])
        self.assertEqual([m["role"] for m in state["messages"]], ["user"])
        self.assertNotIn("tool_result", self.event_types(conversation_id))
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT status FROM pi_tool_calls").fetchone()[0], "discarded")
            [late_evidence] = [r[0] for r in db.execute("SELECT id FROM pi_evidence")]
        with self.assertRaises(RunNotActive):
            self.store.begin_tool_call(run_id, "call1", "search_transport", {})
        with self.assertRaises(NotFound):
            self.store.get_evidence(conversation_id, late_evidence)

    def test_finish_is_idempotent_and_unknown_usage_is_not_zero(self):
        conversation_id, run_id = self.start()
        self.assertEqual(self.store.finish_run(run_id, "failed", stop_reason="model_error"), "failed")
        self.assertEqual(self.store.finish_run(run_id, "completed"), "failed")
        [finished] = [e for e in self.store.events_after(conversation_id) if e["type"] == "run_finished"]
        self.assertEqual(finished["payload"]["usage"], {"complete": False})
        self.assertEqual(self.store.request_cancel(run_id), "failed")

    def test_usage_is_complete_only_with_every_field(self):
        full = {"input": 10, "output": 5, "cacheRead": 0, "cacheWrite": 0}
        cases = [({}, False), ({"input": 10, "output": 5}, False), (full, True),
                 ({**full, "complete": True, "extra": 1}, True),
                 ({**full, "complete": False}, False),
                 ({**full, "output": -1}, False), ({**full, "output": True}, False),
                 ({**full, "cacheWrite": None, "complete": True}, False)]
        for index, (usage, complete) in enumerate(cases):
            with self.subTest(usage=usage):
                conversation_id = self.store.create_conversation()["id"]
                run_id = self.store.submit_message(conversation_id, f"u{index}", "查询")["run"]["id"]
                self.store.finish_run(run_id, "completed", usage=usage)
                [event] = [e for e in self.store.events_after(conversation_id) if e["type"] == "run_finished"]
                self.assertIs(event["payload"]["usage"]["complete"], complete)


class Restart(StoreCase):
    def test_restart_marks_unfinished_runs_interrupted_without_replay(self):
        conversation_id, run_id = self.start()
        self.store.begin_tool_call(run_id, "call1", "search_transport", {"date": "2099-03-05"})
        restarted = ConversationStore(self.path, clock=self.clock)
        self.assertEqual(restarted.recover_interrupted(), [run_id])
        self.assertEqual(restarted.recover_interrupted(), [])
        self.assertEqual(restarted.run_status(run_id), "interrupted")
        self.assertEqual(self.call_status(run_id, "call1"), "unknown")
        [finished] = [e for e in restarted.events_after(conversation_id) if e["type"] == "run_finished"]
        self.assertEqual(finished["payload"]["status"], "interrupted")
        self.assertEqual(finished["payload"]["stop_reason"], "service_restart")
        with self.assertRaises(ToolOutcomeUnknown):
            restarted.begin_tool_call(run_id, "call1", "search_transport", {"date": "2099-03-05"})
        state = restarted.get_state(conversation_id)
        self.assertIsNone(state["active_run"])
        self.assertEqual(len(state["messages"]), 1)
        self.assertTrue(restarted.submit_message(conversation_id, "m2", "继续")["created"])

    def test_entries_survive_restart_in_order_and_update_in_place(self):
        conversation_id, _ = self.start()
        self.store.save_entries(conversation_id, [{"id": "e1", "v": 1}, {"id": "e2", "v": 1}])
        self.store.save_entries(conversation_id, [{"id": "e1", "v": 2}, {"id": "e3", "v": 1}])
        restarted = ConversationStore(self.path, clock=self.clock)
        self.assertEqual(restarted.load_entries(conversation_id),
                         [{"id": "e1", "v": 2}, {"id": "e2", "v": 1}, {"id": "e3", "v": 1}])
        self.assertEqual(restarted.load_entries(restarted.create_conversation()["id"]), [])

    def test_event_cursor_resumes_after_restart(self):
        conversation_id, run_id = self.start()
        cursor = self.store.get_state(conversation_id)["last_event_seq"]
        self.store.append_event(conversation_id, "text_delta", {"text": "你好"}, run_id)
        restarted = ConversationStore(self.path, clock=self.clock)
        [event] = restarted.events_after(conversation_id, cursor)
        self.assertEqual((event["seq"], event["type"], event["payload"]), (cursor + 1, "text_delta", {"text": "你好"}))
        self.assertEqual(restarted.events_after(conversation_id, cursor + 1), [])

    def test_existing_tables_and_rows_are_preserved(self):
        path = Path(self.folder.name) / "legacy.sqlite3"
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE hil_executions (id TEXT PRIMARY KEY, value TEXT)")
            db.execute("INSERT INTO hil_executions VALUES ('old', 'keep')")
        ConversationStore(path, clock=self.clock).create_conversation()
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute("SELECT * FROM hil_executions").fetchall(), [("old", "keep")])


class Isolation(StoreCase):
    def test_two_conversations_run_at_once_without_sharing_content(self):
        first, first_run = self.start(text="上海到三亚", message_id="a1")
        second, second_run = self.start(text="北京到广州", message_id="b1")
        self.store.add_assistant_message(first_run, "三亚结果")
        self.store.add_assistant_message(second_run, "广州结果")
        texts = lambda cid: [m["text"] for m in self.store.get_state(cid)["messages"]]
        self.assertEqual(texts(first), ["上海到三亚", "三亚结果"])
        self.assertEqual(texts(second), ["北京到广州", "广州结果"])
        self.assertNotIn("广州", str(self.store.events_after(first)))
        listed = {c["id"] for c in self.store.list_conversations()}
        self.assertEqual(listed, {first, second})


class Review(StoreCase):
    """Cases added after review of PR #36."""

    def test_finishing_a_run_marks_its_pending_calls_unknown(self):
        for status in ("completed", "failed", "cancelled"):
            with self.subTest(status=status):
                conversation_id, run_id = self.start(message_id=f"m-{status}")
                self.store.begin_tool_call(run_id, "call1", "search_transport", {})
                if status == "cancelled":
                    self.store.request_cancel(run_id)
                self.store.finish_run(run_id, status)
                self.assertEqual(self.call_status(run_id, "call1"), "unknown")
                self.assertIsNone(self.store.complete_tool_call(run_id, "call1", {"status": "ok"},
                                                                [{"facts": {"late": 1}}]))
                self.assertEqual(self.call_status(run_id, "call1"), "discarded")
                self.assertNotIn("tool_result", self.event_types(conversation_id))

    def test_restart_also_clears_pending_calls_under_ended_runs(self):
        conversation_id, run_id = self.start()
        self.store.begin_tool_call(run_id, "call1", "search_transport", {})
        self.store.finish_run(run_id, "completed")
        with closing(sqlite3.connect(self.path)) as db, db:  # simulate a row written by older code
            db.execute("UPDATE pi_tool_calls SET status='pending'")
        ConversationStore(self.path, clock=self.clock).recover_interrupted()
        self.assertEqual(self.call_status(run_id, "call1"), "unknown")

    def test_restart_while_cancelling_is_interrupted(self):
        conversation_id, run_id = self.start()
        self.store.begin_tool_call(run_id, "call1", "search_transport", {})
        self.store.request_cancel(run_id)
        self.assertEqual(ConversationStore(self.path, clock=self.clock).recover_interrupted(), [run_id])
        self.assertEqual(self.store.run_status(run_id), "interrupted")
        self.assertEqual(self.call_status(run_id, "call1"), "unknown")

    def test_same_call_id_in_a_later_run_queries_again(self):
        conversation_id, first = self.start()
        args = {"date": "2099-03-05"}
        self.store.begin_tool_call(first, "call1", "search_transport", args)
        self.store.complete_tool_call(first, "call1", {"price": "800"})
        self.store.finish_run(first, "completed")
        second = self.store.submit_message(conversation_id, "m2", "再查一次")["run"]["id"]
        self.assertIsNone(self.store.begin_tool_call(second, "call1", "search_transport", args))

    def test_evidence_ids_are_generated_and_never_collide(self):
        conversation_id, run_id = self.start()
        self.store.begin_tool_call(run_id, "a", "search_transport", {})
        self.store.begin_tool_call(run_id, "b", "search_transport", {"again": True})
        same = [{"id": "MU1234", "facts": {"flight": "MU1234"}}]
        first = self.store.complete_tool_call(run_id, "a", {}, same)["evidence_ids"]
        second = self.store.complete_tool_call(run_id, "b", {}, same)["evidence_ids"]
        self.assertEqual(len(set(first + second)), 2)
        self.assertNotIn("MU1234", first + second)

    def test_redelivered_result_is_idempotent_and_different_result_conflicts(self):
        conversation_id, run_id = self.start()
        self.store.begin_tool_call(run_id, "call1", "search_transport", {})
        stored = self.store.complete_tool_call(run_id, "call1", {"status": "ok"}, [{"facts": {"a": 1}}])
        self.assertEqual(self.store.complete_tool_call(run_id, "call1", {"status": "ok"}, [{"facts": {"a": 1}}]),
                         stored)
        with self.assertRaises(ToolCallConflict):
            self.store.complete_tool_call(run_id, "call1", {"status": "changed"})
        with self.assertRaises(ToolCallConflict):
            self.store.fail_tool_call(run_id, "call1", "x", "y")
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM pi_evidence").fetchone()[0], 1)

    def test_argument_key_order_does_not_matter(self):
        conversation_id, run_id = self.start()
        self.store.begin_tool_call(run_id, "call1", "search_transport", {"origin": "上海", "date": "2099-03-05"})
        stored = self.store.complete_tool_call(run_id, "call1", {"status": "ok"})
        self.assertEqual(self.store.begin_tool_call(run_id, "call1", "search_transport",
                                                    {"date": "2099-03-05", "origin": "上海"}), stored)

    def test_assistant_message_ids_are_idempotent(self):
        conversation_id, run_id = self.start()
        self.store.add_assistant_message(run_id, "第一段", message_id="a1")
        self.store.add_assistant_message(run_id, "第一段", message_id="a1")
        with self.assertRaises(MessageConflict):
            self.store.add_assistant_message(run_id, "改过的", message_id="a1")
        with self.assertRaises(MessageConflict):
            self.store.add_assistant_message(run_id, "上海到三亚 2099-03-05 的航班", message_id="m1")
        self.assertEqual([m["id"] for m in self.store.get_state(conversation_id)["messages"]], ["m1", "a1"])

    def test_final_reply_commits_with_run_end_and_is_dropped_after_cancel(self):
        conversation_id, run_id = self.start()
        self.assertEqual(self.store.finish_run(run_id, "completed", reply="三亚航班如下"), "completed")
        self.assertEqual([m["role"] for m in self.store.get_state(conversation_id)["messages"]],
                         ["user", "assistant"])
        self.assertEqual(self.event_types(conversation_id)[-2:], ["message", "run_finished"])
        other_id, other_run = self.start(message_id="m-other")
        self.store.request_cancel(other_run)
        self.assertEqual(self.store.finish_run(other_run, "completed", reply="迟到"), "cancelled")
        self.assertEqual([m["role"] for m in self.store.get_state(other_id)["messages"]], ["user"])

    def test_events_are_rejected_for_foreign_or_ended_runs(self):
        conversation_id, run_id = self.start()
        other_id, other_run = self.start(message_id="m-other")
        with self.assertRaises(NotFound):
            self.store.append_event(conversation_id, "text_delta", {"text": "x"}, other_run)
        self.store.finish_run(run_id, "completed")
        with self.assertRaises(RunNotActive):
            self.store.append_event(conversation_id, "text_delta", {"text": "x"}, run_id)
        self.store.append_event(conversation_id, "note", {"text": "conversation-level"})

    def test_entries_are_tagged_with_their_run(self):
        conversation_id, run_id = self.start()
        self.store.save_entries(conversation_id, [{"id": "e1"}])
        self.store.save_entries(conversation_id, [{"id": "e2"}], run_id=run_id)
        self.store.save_entries(conversation_id, [{"id": "e1", "v": 2}])
        self.assertEqual(self.store.load_entries(conversation_id, with_run_id=True),
                         [{"run_id": None, "entry": {"id": "e1", "v": 2}},
                          {"run_id": run_id, "entry": {"id": "e2"}}])
        other_id, other_run = self.start(message_id="m-other")
        with self.assertRaises(NotFound):
            self.store.save_entries(conversation_id, [{"id": "e3"}], run_id=other_run)


class ReviewRound2(StoreCase):
    """Cases added after the second review of PR #36."""

    def second_run(self, conversation_id, first_run, message_id="m2"):
        self.store.finish_run(first_run, "completed")
        return self.store.submit_message(conversation_id, message_id, "再查一次")["run"]["id"]

    def test_entries_keep_the_run_that_first_wrote_them(self):
        conversation_id, first = self.start()
        self.store.save_entries(conversation_id, [{"id": "e1"}, {"id": "e2"}], run_id=first)
        second = self.second_run(conversation_id, first)
        self.store.save_entries(conversation_id, [{"id": "e1"}, {"id": "e2", "v": 2}, {"id": "e3"}],
                                run_id=second)
        self.assertEqual([(r["entry"]["id"], r["run_id"])
                          for r in self.store.load_entries(conversation_id, with_run_id=True)],
                         [("e1", first), ("e2", first), ("e3", second)])

    def test_assistant_message_id_cannot_be_reused_by_a_later_run(self):
        conversation_id, first = self.start()
        self.store.add_assistant_message(first, "好的", message_id="a1")
        second = self.second_run(conversation_id, first)
        with self.assertRaises(MessageConflict):
            self.store.add_assistant_message(second, "好的", message_id="a1")
        with self.assertRaises(MessageConflict):
            self.store.finish_run(second, "completed", reply="好的", reply_id="a1")
        self.assertEqual(self.store.run_status(second), "running")
        self.assertEqual(self.store.add_assistant_message(first, "好的", message_id="a1"), "a1")

    def test_failed_reply_leaves_the_run_running(self):
        conversation_id, run_id = self.start()
        before = self.store.events_after(conversation_id)
        with self.assertRaises(MessageConflict):  # m1 is the user message
            self.store.finish_run(run_id, "completed", reply="三亚航班如下", reply_id="m1")
        self.assertEqual(self.store.run_status(run_id), "running")
        self.assertEqual(self.store.events_after(conversation_id), before)
        self.assertEqual(len(self.store.get_state(conversation_id)["messages"]), 1)

    def test_retrying_finish_with_a_reply_writes_it_once(self):
        conversation_id, run_id = self.start()
        for _ in range(2):
            self.assertEqual(self.store.finish_run(run_id, "completed", reply="三亚航班如下"), "completed")
        self.assertEqual([m["role"] for m in self.store.get_state(conversation_id)["messages"]],
                         ["user", "assistant"])
        self.assertEqual(self.event_types(conversation_id).count("run_finished"), 1)

    def test_blank_final_reply_finishes_the_run_without_a_message(self):
        conversation_id, run_id = self.start()
        self.assertEqual(self.store.finish_run(run_id, "completed", reply="   "), "completed")
        self.assertEqual(len(self.store.get_state(conversation_id)["messages"]), 1)

    def test_evidence_foreign_key_is_enforced_per_run_and_call(self):
        conversation_id, first = self.start()
        self.store.begin_tool_call(first, "call1", "search_transport", {})
        second = self.second_run(conversation_id, first)

        def insert(run_id, call_id):
            with closing(sqlite3.connect(self.path)) as db, db:
                db.execute("PRAGMA foreign_keys=ON")
                db.execute("INSERT INTO pi_evidence VALUES ('ev-x', ?, ?, ?, 't', '{}', NULL)",
                           (conversation_id, run_id, call_id))

        with self.assertRaises(sqlite3.IntegrityError):
            insert(first, "no-such-call")
        with self.assertRaises(sqlite3.IntegrityError):  # call1 exists, but only in the first run
            insert(second, "call1")
        insert(first, "call1")

    def test_result_cannot_carry_its_own_evidence_ids(self):
        conversation_id, run_id = self.start()
        self.store.begin_tool_call(run_id, "call1", "search_transport", {})
        with self.assertRaises(ValueError):
            self.store.complete_tool_call(run_id, "call1", {"status": "ok", "evidence_ids": ["x"]})
        self.assertEqual(self.call_status(run_id, "call1"), "pending")
        stored = self.store.complete_tool_call(run_id, "call1", {"status": "ok"}, [{"facts": {"a": 1}}])
        self.assertEqual(self.store.complete_tool_call(run_id, "call1", {"status": "ok"}, [{"facts": {"a": 2}}]),
                         stored)  # a redelivery is matched on the result only

    def test_schema_version_is_stamped_and_unversioned_pi_tables_are_refused(self):
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT value FROM pi_meta WHERE key='schema_version'").fetchone()[0], "1")
        ConversationStore(self.path, clock=self.clock)  # reopening is fine
        old = Path(self.folder.name) / "old.sqlite3"
        with closing(sqlite3.connect(old)) as db, db:
            db.execute("CREATE TABLE pi_conversations (id TEXT PRIMARY KEY)")
        with self.assertRaises(SchemaMismatch):
            ConversationStore(old, clock=self.clock)
        with closing(sqlite3.connect(old)) as db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='pi_meta'").fetchone())
        newer = Path(self.folder.name) / "newer.sqlite3"
        ConversationStore(newer, clock=self.clock)
        with closing(sqlite3.connect(newer)) as db, db:
            db.execute("UPDATE pi_meta SET value='2'")
        with self.assertRaises(SchemaMismatch):
            ConversationStore(newer, clock=self.clock)


if __name__ == "__main__":
    unittest.main()
