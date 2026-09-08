import copy
import json
from pathlib import Path
import tempfile
import unittest

from travel_agent.agent import Limits, plan_trip
from travel_agent.domain import custom_scenario, normalize_request, scenario_by_id, validate_verdict
from travel_agent.evaluation import evaluate, grade
from travel_agent.providers import Baseline, tool_call
from travel_agent.telemetry import Store
from travel_agent.tools import Tools, TOOL_NAMES

ROOT = Path(__file__).resolve().parent.parent


class TravelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "test.sqlite3")
        self.scenario = scenario_by_id("hangzhou-shanghai")
        self.request = normalize_request(self.scenario["request"])
        self.case = json.loads((ROOT / "evals/cases.json").read_text(encoding="utf-8"))[0]

    def run_case(self, scenario=None, provider=None, **kwargs):
        return plan_trip(scenario or self.scenario, provider or Baseline(), self.store, **kwargs)

    def test_user_example_values_and_trace(self):
        run = self.run_case()
        verdict = run["verdict"]
        self.assertEqual(verdict["status"], "planned")
        self.assertEqual(verdict["total_cny"], 628)
        self.assertEqual(verdict["itinerary"][0]["start_at"], "2026-09-11T19:00+08:00")
        self.assertEqual(verdict["itinerary"][-1]["end_at"], "2026-09-12T07:50+08:00")
        self.assertEqual([x["kind"] for x in verdict["itinerary"]],
                         ["intercity", "station_to_hotel", "hotel", "hotel_to_meeting"])
        trace = self.store.get_run(run["trace_id"])
        root = next(s for s in trace["spans"] if s["name"] == "invoke_agent")
        children = [s for s in trace["spans"] if s["name"] != "invoke_agent"]
        self.assertTrue(all(s["parent_span_id"] == root["span_id"] for s in children))
        self.assertEqual(len([s for s in children if s["name"] == "execute_tool"]), 4)
        self.assertEqual(len([s for s in children if s["name"] == "validate_result"]), 1)

    def test_all_regression_cases_have_independent_traces(self):
        report = evaluate(self.store, Baseline())
        self.assertEqual((report["total"], report["passed"], report["gate_passed"]), (6, 6, True))
        self.assertEqual(len({row["trace_id"] for row in report["rows"]}), 6)
        for row in report["rows"]:
            self.assertEqual(self.store.get_run(row["trace_id"])["evaluation_id"], report["id"])

    def test_recovery_and_terminal_timeout_are_visible(self):
        for name, expected in (("retry-once", "planned"), ("tool-timeout", "needs_review")):
            run = self.run_case(scenario_by_id(name))
            self.assertEqual(run["verdict"]["status"], expected)
            self.assertEqual(run["retries"], 1)
            errors = [s for s in self.store.get_run(run["trace_id"])["spans"] if s["status"] == "error"]
            self.assertEqual(len(errors), 1 if name == "retry-once" else 2)

    def test_runtime_rejects_forged_citations_and_reordered_plan(self):
        valid = self.run_case()["verdict"]
        tools = Tools(self.scenario, self.request)
        for name in TOOL_NAMES:
            tools.execute(name, {})
        base = {k: valid[k] for k in ("status", "reason", "segment_ids", "evidence_ids")}
        forged = copy.deepcopy(base)
        forged["evidence_ids"].append("nonexistent-hotel")
        reordered = copy.deepcopy(base)
        reordered["segment_ids"].reverse()
        for value in (forged, reordered):
            with self.assertRaises(ValueError):
                validate_verdict(value, tools.seen, self.request)

    def test_runtime_rejects_bad_time_budget_and_location_even_with_valid_ids(self):
        valid = self.run_case()["verdict"]
        args = {k: valid[k] for k in ("status", "reason", "segment_ids", "evidence_ids")}
        for identifier, key, value in (("taxi-morning", "end_at", "2026-09-12T08:10+08:00"),
                                       ("hotel-demo", "price_cny", 650),
                                       ("taxi-evening", "from", "错误车站")):
            tools = Tools(self.scenario, self.request)
            for name in TOOL_NAMES:
                tools.execute(name, {})
            tools.seen[identifier][key] = value
            with self.assertRaises(ValueError):
                validate_verdict(args, tools.seen, self.request)

    def test_model_failure_is_not_replaced_with_baseline_and_does_not_leak(self):
        class Broken:
            name, model = "deepseek", "test-failure"
            def chat(self, *args):
                raise RuntimeError("SECRET-MUST-NOT-LEAK")
        run = self.run_case(provider=Broken())
        self.assertEqual((run["provider"], run["termination"]), ("deepseek", "provider_error"))
        self.assertFalse(run["usage_complete"])
        self.assertIsNone(run["cost_usd"])
        self.assertNotIn("SECRET-MUST-NOT-LEAK", json.dumps(self.store.get_run(run["trace_id"])))

    def test_budget_limits_stop_loop(self):
        for limits, reason in ((Limits(max_steps=1), "steps"), (Limits(max_tool_calls=0), "tool_calls"),
                               (Limits(max_tokens=0), "tokens"), (Limits(timeout_s=0), "deadline")):
            run = self.run_case(limits=limits)
            self.assertEqual(run["termination"], "budget_exceeded")
            self.assertEqual(run["budget_reason"], reason)
            self.assertTrue(any(s["attributes"].get("budget.reason") == reason
                                for s in self.store.get_run(run["trace_id"])["spans"]))
            self.assertEqual(run["verdict"]["itinerary"], [])

    def test_unqueried_failure_cannot_pass_negative_cases(self):
        class Refuse(Baseline):
            def chat(self, *args):
                return {"message": tool_call("finish", {"status": "unavailable", "reason": "no_feasible_route",
                        "segment_ids": [], "evidence_ids": []}), "input_tokens": 0, "output_tokens": 0}
        report = evaluate(self.store, Refuse())
        self.assertEqual(report["passed"], 0)
        for row in report["rows"]:
            self.assertEqual(row["termination"], "invalid_response")
            self.assertFalse(row["checks"]["required_tools"])

    def test_unknown_tool_and_early_fabricated_finish_are_blocked(self):
        class Bad:
            name, model = "baseline", "bad-test"
            def __init__(self, reply): self.reply = reply
            def chat(self, *args):
                return {"message": self.reply, "input_tokens": 0, "output_tokens": 0}
        for reply in (tool_call("book_and_pay"), tool_call("finish", {
                "status": "planned", "reason": "feasible", "segment_ids": [], "evidence_ids": []})):
            run = self.run_case(provider=Bad(reply))
            self.assertEqual(run["termination"], "invalid_response")
            self.assertEqual(run["guardrail_blocks"], 1)

    def test_custom_date_rollover_and_unsupported_city(self):
        request = {**self.scenario["request"], "meeting_at": "2027-01-01T08:00"}
        run = self.run_case(custom_scenario(request))
        self.assertEqual(run["verdict"]["itinerary"][0]["start_at"], "2026-12-31T19:00+08:00")
        self.assertEqual(run["verdict"]["itinerary"][-1]["end_at"], "2027-01-01T07:50+08:00")
        run = self.run_case(custom_scenario({**request, "origin": "北京"}))
        self.assertEqual(run["verdict"]["status"], "unavailable")
        for invalid in ("0001-01-01T08:00", "9999-12-31T23:00-12:00"):
            with self.assertRaises(ValueError):
                custom_scenario({**request, "meeting_at": invalid})

    def test_oracle_is_not_sent_to_model(self):
        class Recording(Baseline):
            def chat(inner, messages, schemas, timeout):
                content = json.dumps(messages)
                for key in ("required_evidence", "expected_itinerary", "expected_retries", "arrival_deadline", "split", "oracle"):
                    self.assertNotIn(key, content)
                return super().chat(messages, schemas, timeout)
        self.assertTrue(evaluate(self.store, Recording())["gate_passed"])

    def test_grader_detects_mutated_output_and_inverted_oracle(self):
        run = self.run_case()
        self.assertTrue(grade(run, self.case)["passed"])
        for field, value in (("total_cny", 627), ("status", "unavailable"), ("evidence_ids", ["invented"])):
            bad = copy.deepcopy(run)
            bad["verdict"][field] = value
            self.assertFalse(grade(bad, self.case)["passed"], field)
        inverted = {**self.case, "status": "unavailable"}
        self.assertFalse(grade(run, inverted)["checks"]["status"])
        for index, field, value in ((0, "seat_class", "商务座"), (1, "mode", "直升机"),
                                    (2, "label", "虚构酒店")):
            bad = copy.deepcopy(run)
            bad["verdict"]["itinerary"][index][field] = value
            self.assertFalse(grade(bad, self.case)["safe"])
        bad = copy.deepcopy(run)
        bad["verdict"]["itinerary"][0]["price_cny"] += 20
        bad["verdict"]["itinerary"][2]["price_cny"] -= 20
        self.assertFalse(grade(bad, self.case)["passed"])


if __name__ == "__main__":
    unittest.main()
