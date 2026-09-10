import json
from pathlib import Path
import tempfile
import unittest

from travel_agent.agent import plan_trip
from travel_agent.domain import scenario_by_id
from travel_agent.providers import tool_call
from travel_agent.telemetry import Store


POLICY_HASH = "14e89ab8c38fa908dd9652cff0ba5310dd491f6d070736c70a27ce418555c49d"


class PolicyModel:
    name, model = "offline", "policy-contract"

    def chat(self, messages, schemas, timeout):
        results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        if not results:
            message = tool_call("get_policy_snapshot")
        else:
            message = tool_call("finish", {"status": "needs_review", "reason": "missing_data",
                "segment_ids": [], "evidence_ids": results[-1]["evidence_refs"]})
        return {"message": message, "input_tokens": 0, "output_tokens": 0}


class PolicyTests(unittest.TestCase):
    def test_policy_query_completes_with_traceable_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "trace.sqlite3")
            run = plan_trip(scenario_by_id("hangzhou-shanghai"), PolicyModel(), store, tool_mode="real")
            self.assertEqual(run["termination"], "completed")
            self.assertEqual(run["verdict"]["reason"], "missing_data")
            self.assertEqual(run["verdict"]["itinerary"], [])
            self.assertIsNone(run["verdict"]["total_cny"])
            evidence = run["evidence"][0]
            self.assertEqual(evidence["data"]["content_hash"], POLICY_HASH)
            self.assertNotIn("experiments", evidence["data"]["source_ref"])
            self.assertEqual(run["verdict"]["evidence_ids"], [evidence["id"]])
            trace = store.get_run(run["trace_id"])
            span = next(s for s in trace["spans"] if s["name"] == "execute_tool")
            self.assertEqual(span["attributes"]["content_hash"], evidence["data"]["content_hash"])
            self.assertEqual(span["attributes"]["tool_status"], "ok")

    def test_missing_policy_is_reported_without_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "trace.sqlite3")
            run = plan_trip(scenario_by_id("hangzhou-shanghai"), PolicyModel(), store,
                            tool_mode="real", policy_path=Path(directory) / "missing.md")
            self.assertEqual(run["termination"], "completed")
            self.assertEqual(run["status"], "needs_review")
            self.assertEqual(run["evidence"], [])
            self.assertEqual(run["tool_calls"], 1)
            result = run["tool_results"][0]
            self.assertEqual(result["status"], "error")
            self.assertEqual(result["error"]["type"], "PolicyReadError")
            self.assertFalse(result["error"]["retryable"])
            self.assertIsNone(result["data"])

    def test_repeated_queries_preserve_both_evidence_records(self):
        class Repeat(PolicyModel):
            def chat(self, messages, schemas, timeout):
                results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
                if len(results) < 2:
                    message = tool_call("get_policy_snapshot")
                else:
                    message = tool_call("finish", {"status": "needs_review", "reason": "missing_data",
                        "segment_ids": [], "evidence_ids": [r["evidence_refs"][0] for r in results]})
                return {"message": message, "input_tokens": 0, "output_tokens": 0}
        with tempfile.TemporaryDirectory() as directory:
            run = plan_trip(scenario_by_id("hangzhou-shanghai"), Repeat(),
                            Store(Path(directory) / "trace.sqlite3"), tool_mode="real")
            self.assertEqual(run["termination"], "completed")
            self.assertEqual(len(run["tool_results"]), 2)
            self.assertEqual(len(set(run["verdict"]["evidence_ids"])), 2)
            self.assertEqual(len(run["evidence"]), 2)

    def test_early_missing_data_allowed_but_false_conclusions_blocked(self):
        class Finish(PolicyModel):
            def __init__(self, status, reason, refs):
                self.args = {"status": status, "reason": reason, "segment_ids": [], "evidence_ids": refs}
            def chat(self, *args):
                return {"message": tool_call("finish", self.args), "input_tokens": 0, "output_tokens": 0}
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "trace.sqlite3")
            for status, reason, refs, termination in [
                ("needs_review", "missing_data", [], "completed"),
                ("needs_review", "missing_data", ["foreign-run:policy"], "invalid_response"),
                ("planned", "feasible", [], "invalid_response"),
                ("unavailable", "no_feasible_route", [], "invalid_response")]:
                with self.subTest(reason=reason, refs=refs):
                    run = plan_trip(scenario_by_id("hangzhou-shanghai"), Finish(status, reason, refs),
                                    store, tool_mode="real")
                    self.assertEqual(run["termination"], termination)
                    self.assertEqual(run["tool_calls"], 0)

    def test_corrupt_policy_is_not_success(self):
        with tempfile.TemporaryDirectory() as directory:
            policy = Path(directory) / "broken.md"
            policy.write_bytes(b"not a regulation")
            run = plan_trip(scenario_by_id("hangzhou-shanghai"), PolicyModel(),
                            Store(Path(directory) / "trace.sqlite3"), tool_mode="real", policy_path=policy)
            self.assertEqual(run["tool_results"][0]["status"], "error")
            self.assertEqual(run["evidence"], [])

    def test_policy_hash_assertion_detects_changed_source_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            from travel_agent.domain import PACKAGE
            policy = Path(directory) / "changed.md"
            policy.write_bytes((PACKAGE / "policies/浙江省机关工作人员差旅费管理规定.md").read_bytes() + b"\n")
            run = plan_trip(scenario_by_id("hangzhou-shanghai"), PolicyModel(),
                            Store(Path(directory) / "trace.sqlite3"), tool_mode="real", policy_path=policy)
            self.assertEqual(run["termination"], "completed")
            self.assertNotEqual(run["evidence"][0]["data"]["content_hash"], POLICY_HASH)
