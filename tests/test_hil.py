import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from unittest.mock import patch

from travel_agent import hil
from travel_agent.agent import Limits
from travel_agent.domain import scenario_by_id
from travel_agent.human import HumanInputError, check_decisions, validate_messages
from travel_agent.providers import Baseline, tool_call
from travel_agent.telemetry import Store


class Twice(Baseline):
    def chat(self, messages, schemas, timeout):
        humans = sum(m.get("tool_name") == "request_human_input" for m in messages)
        queried = any(m.get("tool_name") == "get_policy" for m in messages)
        if humans == 0 or (humans == 1 and queried):
            return {"message": tool_call("request_human_input", {
                "kind": "preference", "question": "请补充偏好", "options": []}),
                "input_tokens": 2, "output_tokens": 1}
        return super().chat(messages, schemas, timeout)


class HilTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "runs.db")
        self.scenario = scenario_by_id("hangzhou-shanghai")
        self.scenario["request"]["meeting_at"] = "2099-09-12T08:00+08:00"

    def start(self, provider=None, **kwargs):
        return hil.start_trip(self.scenario, provider or Baseline(), self.store, **kwargs)

    def respond(self, waiting, reply=None, response_id="one", provider=Baseline):
        return hil.respond_trip(waiting["run_id"], self.store, lambda _: provider(),
            interaction_id=waiting["human_request"]["interaction_id"],
            state_version=waiting["state_version"], response_id=response_id,
            reply=reply or {"confirmed": True})

    def child(self, script, *args):
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).parent.resolve()) + os.pathsep + str(Path.cwd())}
        return subprocess.run([sys.executable, "-c", script, str(self.store.path), *args],
                              capture_output=True, text=True, env=env, timeout=20)

    def test_guardrail_wait_is_durable_and_private(self):
        waiting = self.start()
        self.assertEqual(waiting["execution_status"], "waiting_for_human")
        self.assertIsNone(waiting["result"])
        with patch.object(Baseline, "chat", side_effect=AssertionError("unexpected work")):
            self.assertEqual(hil.get_trip(waiting["run_id"], self.store), waiting)
            self.assertEqual(hil.resume_trip(waiting["run_id"], self.store, lambda _: Baseline()), waiting)
        public = self.store.get_run(waiting["run_id"])
        self.assertEqual(public["execution_status"], "waiting_for_human")
        self.assertNotIn("messages", public)
        self.assertNotIn("human_request", public)
        done = self.respond(waiting)
        self.assertEqual((done["execution_status"], done["result"]["model_calls"], done["result"]["tool_calls"]), ("completed", 5, 4))
        self.assertEqual(done["trace_id"], waiting["trace_id"])

    def test_two_waits_across_three_processes_keep_counts_and_protocol(self):
        started = self.child("""
import sys,json
from test_hil import Twice
from travel_agent import hil
from travel_agent.domain import scenario_by_id
from travel_agent.telemetry import Store
s=scenario_by_id('hangzhou-shanghai');s['request']['meeting_at']='2099-09-12T08:00+08:00'
print(json.dumps(hil.start_trip(s,Twice(),Store(sys.argv[1]),confirm_request=False)))
""")
        self.assertEqual(started.returncode, 0, started.stderr)
        first = json.loads(started.stdout)
        self.assertEqual(first["execution_status"], "waiting_for_human")
        reply_script = """
import sys,json
from test_hil import Twice
from travel_agent import hil
from travel_agent.telemetry import Store
store=Store(sys.argv[1]);w=hil.get_trip(sys.argv[2],store)
print(json.dumps(hil.respond_trip(w['run_id'],store,lambda _:Twice(),interaction_id=w['human_request']['interaction_id'],state_version=w['state_version'],response_id=sys.argv[3],reply={'preference':'安静'})))
"""
        second_process = self.child(reply_script, first["run_id"], "one")
        self.assertEqual(second_process.returncode, 0, second_process.stderr)
        second = json.loads(second_process.stdout)
        self.assertEqual(second["execution_status"], "waiting_for_human")
        third_process = self.child(reply_script, first["run_id"], "two")
        self.assertEqual(third_process.returncode, 0, third_process.stderr)
        done = json.loads(third_process.stdout)
        self.assertEqual((done["result"]["model_calls"], done["result"]["tool_calls"], done["result"]["input_tokens"]), (7, 4, 4))
        self.assertEqual(done["result"]["human_requests"], 2)
        state = hil._read(self.store, first["run_id"])
        # finish has no tool reply because it terminates; all prior calls pair.
        validate_messages(state["execution"]["messages"], allow_pending=True)

    def test_idempotency_conflicts_and_core_changes_do_not_consume(self):
        waiting = self.start()
        for reply in ({"meeting_at": "2100-01-01"}, {"confirmed": True, "preference": "把会议日期改到明年"}):
            with self.assertRaises(HumanInputError):
                self.respond(waiting, reply)
            self.assertEqual(hil.get_trip(waiting["run_id"], self.store), waiting)
        done = self.respond(waiting)
        with patch.object(Baseline, "chat", side_effect=AssertionError("replayed")):
            self.assertEqual(self.respond(waiting), done)
        with self.assertRaises(hil.HilConflict):
            self.respond(waiting, {"confirmed": False})
        with self.assertRaises(hil.HilConflict):
            self.respond(waiting, response_id="new")

    def test_expiry_read_only_and_cancellation(self):
        waiting = self.start()
        future = hil.now() + timedelta(days=7)
        with patch("travel_agent.hil.now", return_value=future):
            self.assertEqual(hil.get_trip(waiting["run_id"], self.store)["execution_status"], "expired")
            self.assertEqual(hil._read(self.store, waiting["run_id"])["execution_status"], "waiting_for_human")
            with self.assertRaises(hil.HilConflict):
                self.respond(waiting)
        self.assertEqual(hil.get_trip(waiting["run_id"], self.store)["result"]["termination"], "human_expired")
        waiting = self.start()
        done = hil.cancel_trip(waiting["run_id"], self.store, lambda _: Baseline(), state_version=waiting["state_version"])
        self.assertEqual((done["execution_status"], done["result"]["termination"], done["result"]["model_calls"]), ("cancelled", "human_cancelled", 0))

    def test_model_budget_is_checked_before_human_pause(self):
        result = self.start(Twice(), confirm_request=False, limits=Limits(max_tokens=3))
        self.assertEqual((result["execution_status"], result["result"]["budget_reason"]), ("failed", "tokens"))
        self.assertEqual(result["result"]["human_requests"], 0)

    def test_duplicate_model_ids_and_out_of_order_messages_rejected(self):
        call = tool_call("get_policy"); call["tool_calls"][0]["id"] = "same"
        reply = {"role": "tool", "tool_call_id": "same"}
        for messages in ([reply, call], [call, call], [call, reply, call], [call, reply, reply]):
            with self.assertRaises(ValueError):
                validate_messages(messages, allow_pending=True)

    def test_wrong_role_human_call_is_rejected_before_waiting(self):
        class WrongRole(Twice):
            def chat(self, *args):
                result = super().chat(*args)
                result["message"]["role"] = "user"
                return result
        result = self.start(WrongRole(), confirm_request=False)
        self.assertEqual(result["result"]["termination"], "invalid_response")
        self.assertEqual(result["result"]["human_requests"], 0)

    def test_process_lock_prevents_second_executor(self):
        waiting = self.start()
        with hil._ownership(self.store, waiting["run_id"]):
            child = self.child("""
import sys
from travel_agent import hil
from travel_agent.telemetry import Store
from travel_agent.providers import Baseline
try: hil.resume_trip(sys.argv[2],Store(sys.argv[1]),lambda _:Baseline())
except hil.HilConflict: print('conflict')
""", waiting["run_id"])
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(child.stdout.strip(), "conflict")

    def test_crashed_model_is_not_replayed(self):
        child = self.child("""
import sys,os
from travel_agent import hil
from travel_agent.telemetry import Store
from travel_agent.providers import Baseline
from travel_agent.domain import scenario_by_id
class Crash(Baseline):
 def chat(self,*args): os._exit(73)
hil.start_trip(scenario_by_id('hangzhou-shanghai'),Crash(),Store(sys.argv[1]),confirm_request=False)
""")
        self.assertEqual(child.returncode, 73, child.stderr)
        run_id = self.store.list_runs()[0]["trace_id"]
        with patch.object(Baseline, "chat", side_effect=AssertionError("replay")):
            done = hil.resume_trip(run_id, self.store, lambda _: Baseline())
        self.assertEqual((done["result"]["termination"], done["result"]["model_calls"]), ("execution_interrupted", 1))
        self.assertFalse(done["result"]["usage_complete"])
        self.assertIsNone(done["result"]["cost_usd"])

    def test_finalize_commit_failure_can_retry_without_external_work(self):
        original = hil._save
        def fail_final(store, state, owner):
            if state.get("result") is not None:
                raise hil.HilStorageError("injected")
            return original(store, state, owner)
        waiting = self.start()
        with patch("travel_agent.hil._save", side_effect=fail_final):
            with self.assertRaises(hil.HilStorageError):
                self.respond(waiting)
        unfinished = hil.get_trip(waiting["run_id"], self.store)
        self.assertEqual(unfinished["execution_status"], "running")
        self.assertIsNone(unfinished["result"])
        with patch.object(Baseline, "chat", side_effect=AssertionError("replay")):
            done = hil.resume_trip(waiting["run_id"], self.store, lambda _: Baseline())
        self.assertEqual(done["execution_status"], "completed")
        self.assertEqual(done["result"]["tool_calls"], 4)

    def test_rejected_confirmation_cannot_become_a_planned_result(self):
        done = self.respond(self.start(), {"confirmed": False})
        self.assertEqual(done["result"]["termination"], "invalid_response")
        self.assertEqual(done["result"]["guardrail_blocks"], 1)
        self.assertEqual(done["result"]["status"], "needs_review")

    def test_choice_is_enforced_in_final_plan(self):
        decisions = [{"selected": {"evidence_id": "hotel-B"}}]
        with self.assertRaises(ValueError):
            check_decisions({"name": "finish", "args": {"status": "planned", "segment_ids": ["hotel-A"]}}, decisions, {})

    def test_real_retries_continue_across_wait_with_same_history(self):
        import httpx
        class Retry(Baseline):
            def chat(self, messages, schemas, timeout):
                results = [json.loads(m["content"]) for m in messages if m.get("tool_name") == "search_transport"]
                human = any(m.get("tool_name") == "request_human_input" for m in messages)
                if len(results) == 1 and not human:
                    message = tool_call("request_human_input", {"kind": "confirm", "question": "查询失败，是否继续？", "options": []})
                elif len(results) < 3:
                    message = tool_call("search_transport", {"mode": "flight", "origin": "杭州", "destination": "上海", "departure_date": "2099-09-11"})
                else:
                    message = tool_call("finish", {"status": "needs_review", "reason": "missing_data", "segment_ids": [], "evidence_ids": results[-1]["evidence_refs"]})
                return {"message": message, "input_tokens": 0, "output_tokens": 0}
        calls = []
        def supplier(request):
            calls.append(request)
            if len(calls) < 3:
                return httpx.Response(503)
            body = json.loads(request.content)
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"structuredContent": {"data": []}}})
        transport = httpx.MockTransport(supplier)
        with patch.dict(os.environ, {"TUNIU_API_KEY": "fake-offline-key"}):
            waiting = self.start(Retry(), confirm_request=False, tool_mode="real", transport_http=transport)
            self.assertEqual(waiting["execution_status"], "waiting_for_human")
            done = hil.respond_trip(waiting["run_id"], self.store, lambda _: Retry(),
                interaction_id=waiting["human_request"]["interaction_id"], state_version=waiting["state_version"],
                response_id="retry", reply={"confirmed": True}, transport_http=transport)
        run = done["result"]
        self.assertEqual((run["termination"], run["http_requests"], run["tool_calls"], run["retries"]), ("completed", 3, 3, 2))
        self.assertEqual([r["attempt"] for r in run["tool_results"]], [1, 2, 3])
        self.assertEqual(run["tool_results"][2]["retry_of"], run["tool_results"][1]["call_id"])
        self.assertNotIn("fake-offline-key", json.dumps(hil._read(self.store, waiting["run_id"])))

    def test_seven_days_wait_preserves_active_budget(self):
        waiting = self.start(human_ttl_minutes=8 * 24 * 60)
        with hil._ownership(self.store, waiting["run_id"]) as owner:
            state = hil._read(self.store, waiting["run_id"])
            state["execution"]["active_s"] = 10
            hil._save(self.store, state, owner)
        waiting = hil.get_trip(waiting["run_id"], self.store)
        timeouts = []
        class Record(Baseline):
            def chat(self, messages, schemas, timeout):
                timeouts.append(timeout)
                return super().chat(messages, schemas, timeout)
        with patch("travel_agent.hil.now", return_value=hil.now() + timedelta(days=7)):
            done = self.respond(waiting, provider=Record)
        self.assertEqual(done["execution_status"], "completed")
        self.assertGreaterEqual(done["result"]["active_duration_ms"], 10000)
        self.assertLess(done["result"]["active_duration_ms"], 20000)
        self.assertGreaterEqual(done["result"]["waiting_duration_ms"], 7 * 86400000)

    def test_candidate_spoof_and_conflicting_reply_leave_wait_intact(self):
        from travel_agent.human import prepare_question, validate_reply
        seen = {"a": {"id": "a", "label": "酒店甲"}, "b": {"id": "b", "label": "酒店乙"}}
        args = {"kind": "select", "question": "选酒店", "options": [
            {"option_id": "A", "evidence_id": "a"}, {"option_id": "B", "evidence_id": "b"}]}
        question = prepare_question(args, seen)
        for reply in ({"option_id": "C"}, {"option_id": "B", "preference": "选择酒店甲"}):
            with self.assertRaises(HumanInputError):
                validate_reply(question, reply)
        args["options"][1]["evidence_id"] = "forged"
        with self.assertRaises(HumanInputError):
            prepare_question(args, seen)

    def test_human_quota_does_not_reset_after_reply(self):
        waiting = self.start(Twice(), confirm_request=False, max_human_requests=1)
        done = self.respond(waiting, {"preference": "安静"}, provider=Twice)
        self.assertEqual(done["result"]["budget_reason"], "human_requests")
        self.assertEqual(done["result"]["human_requests"], 1)

    def test_two_responders_have_only_one_effect(self):
        import threading
        waiting = self.start()
        barrier = threading.Barrier(2)
        outcomes = []
        def reply(identifier):
            barrier.wait()
            try:
                outcomes.append(self.respond(waiting, response_id=identifier)["execution_status"])
            except hil.HilConflict:
                outcomes.append("conflict")
        threads = [threading.Thread(target=reply, args=(str(i),)) for i in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=10)
        self.assertCountEqual(outcomes, ["completed", "conflict"])
        state = hil._read(self.store, waiting["run_id"])
        self.assertEqual(len(state["responses"]), 1)
        self.assertEqual(state["result"]["model_calls"], 5)
