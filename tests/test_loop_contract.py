"""Pre-refactor behavioral gates: expected values come from the legacy contract."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from travel_agent.agent import Limits, plan_trip
from travel_agent.domain import scenario_by_id
from travel_agent.providers import Baseline, tool_call
from travel_agent.telemetry import Store


class Scripted:
    name, model = "test", "contract"
    pricing = {"input": 1}

    def __init__(self, response):
        self.response = response

    def chat(self, *args):
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class LoopContractTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name) / "runs.db")
        self.scenario = scenario_by_id("hangzhou-shanghai")

    def run_reply(self, message, **kwargs):
        return plan_trip(self.scenario, Scripted({"message": message,
            "input_tokens": 7, "output_tokens": 3, "cache_hit_tokens": 2,
            "cost_usd": .25}), self.store, **kwargs)

    def test_provider_exception_classification_and_unknown_usage(self):
        for error in (ValueError, TypeError, KeyError, IndexError, RuntimeError):
            with self.subTest(error=error):
                run = plan_trip(self.scenario, Scripted(error("private")), self.store)
                self.assertEqual(run["termination"], "provider_error" if error is RuntimeError else "invalid_response")
                self.assertEqual(run["model_calls"], 1)
                self.assertEqual(run["tool_calls"], 0)
                self.assertFalse(run["usage_complete"])
                self.assertIsNone(run["cost_usd"])

    def test_invalid_structure_keeps_usage_and_guardrail_semantics(self):
        for message, blocks in (({"tool_calls": []}, 0),
                                (tool_call(123), 0), (tool_call("unknown"), 1),
                                ({"tool_calls": [{}]}, 0)):
            with self.subTest(message=message):
                run = self.run_reply(message)
                self.assertEqual(run["termination"], "invalid_response")
                self.assertEqual(run["guardrail_blocks"], blocks)
                self.assertEqual((run["input_tokens"], run["output_tokens"], run["cache_hit_tokens"]), (7, 3, 2))
                self.assertEqual(run["cost_usd"], .25)
                self.assertEqual(run["tool_calls"], 0)

    def test_post_response_tokens_prevent_dispatch_even_finish(self):
        for name in ("get_policy", "finish"):
            run = self.run_reply(tool_call(name), limits=Limits(max_tokens=10))
            self.assertEqual((run["termination"], run["budget_reason"]), ("budget_exceeded", "tokens"))
            self.assertEqual((run["model_calls"], run["tool_calls"]), (1, 0))

    def test_last_step_finish_is_allowed(self):
        run = plan_trip(self.scenario, Baseline(), self.store, Limits(max_steps=5))
        self.assertEqual((run["termination"], run["model_calls"], run["tool_calls"]), ("completed", 5, 4))

    def test_retry_is_atomic_and_second_attempt_checks_tool_budget(self):
        scenario = scenario_by_id("retry-once")
        run = plan_trip(scenario, Baseline(), self.store, Limits(retry_delay_s=0))
        self.assertEqual((run["termination"], run["model_calls"], run["tool_calls"], run["retries"]), ("completed", 5, 5, 1))
        scenario["faults"] = {"get_policy": 1}
        run = plan_trip(scenario, Baseline(), self.store, Limits(max_tool_calls=1, retry_delay_s=0))
        self.assertEqual((run["budget_reason"], run["model_calls"], run["tool_calls"], run["retries"]), ("tool_calls", 1, 1, 1))

    def test_tool_value_error_and_other_exception(self):
        for error, termination, blocks in ((ValueError, "invalid_response", 1), (RuntimeError, "provider_error", 0)):
            with patch("travel_agent.agent.Tools.execute", side_effect=error("private")):
                run = self.run_reply(tool_call("get_policy"))
            self.assertEqual((run["termination"], run["guardrail_blocks"]), (termination, blocks))

    def test_initialization_and_final_save_errors_propagate(self):
        with self.assertRaises(ValueError):
            plan_trip(self.scenario, Baseline(), self.store, tool_mode="bad")
        original = self.store.save_run
        def save(run):
            if run["termination"] is not None:
                raise OSError("disk full")
            original(run)
        with patch.object(self.store, "save_run", side_effect=save):
            with self.assertRaises(OSError):
                plan_trip(self.scenario, Baseline(), self.store)
