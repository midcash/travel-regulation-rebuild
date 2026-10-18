import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from travel_agent.agent import BudgetExceeded, Limits, TripExecution, plan_trip
from travel_agent.domain import scenario_by_id
from travel_agent.providers import Baseline
from travel_agent.telemetry import Store


class ExecutionStateTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name) / "runs.db")
        self.scenario = scenario_by_id("hangzhou-shanghai")

    def test_roundtrip_every_boundary_matches_legacy_result(self):
        expected = plan_trip(self.scenario, Baseline(), self.store)
        execution = TripExecution(self.scenario, Baseline(), self.store)
        identity = execution.run["trace_id"]
        while execution.stage != "finalize":
            execution.advance()
            state = json.loads(json.dumps(execution.snapshot()))
            execution = TripExecution.restore(state, Baseline(), self.store)
        actual = execution.finalize()
        for key in ("verdict", "draft", "termination", "model_calls", "tool_calls",
                    "input_tokens", "output_tokens", "retries", "guardrail_blocks",
                    "observed_evidence_ids", "observed_tool_names"):
            self.assertEqual(actual[key], expected[key], key)
        self.assertEqual(actual["trace_id"], identity)

    def test_pending_dispatch_does_not_call_model_and_retry_stays_atomic(self):
        scenario = {**self.scenario, "faults": {"get_policy": 1}}
        execution = TripExecution(scenario, Baseline(), self.store, Limits(retry_delay_s=0))
        execution.advance()
        restored = TripExecution.restore(execution.snapshot(), Baseline(), self.store)
        with patch.object(restored.provider, "chat", side_effect=AssertionError("model replay")):
            restored.advance()
        self.assertEqual((restored.stage, restored.toolbox.calls, restored.run["retries"], restored.run["model_calls"]),
                         ("model_turn", 2, 1, 1))
        self.assertEqual(len(restored.messages), 4)
        self.assertEqual(restored.toolbox._faults["get_policy"], 0)

    def test_offline_time_does_not_reset_or_consume_active_budget(self):
        with patch("travel_agent.agent.time.perf_counter", return_value=100):
            execution = TripExecution(self.scenario, Baseline(), self.store)
        with patch("travel_agent.agent.time.perf_counter", return_value=110):
            snapshot = execution.snapshot()
        with patch("travel_agent.agent.time.perf_counter", return_value=999999):
            restored = TripExecution.restore(snapshot, Baseline(), self.store)
            self.assertEqual(restored.deadline - 999999, 170)
        with patch("travel_agent.agent.time.perf_counter", return_value=1000169):
            with self.assertRaises(BudgetExceeded) as raised:
                restored.advance()
            self.assertEqual(raised.exception.reason, "deadline")

    def test_unknown_cost_is_sticky_and_incompatible_snapshot_rejected(self):
        execution = TripExecution(self.scenario, Baseline(), self.store)
        execution.run["cost_usd"] = None
        state = execution.snapshot()
        restored = TripExecution.restore(state, Baseline(), self.store)
        restored.advance()
        self.assertIsNone(restored.run["cost_usd"])
        state["schema_version"] = 99
        with self.assertRaises(ValueError):
            TripExecution.restore(state, Baseline(), self.store)

    def test_real_tool_state_roundtrip_preserves_retry_links(self):
        from travel_agent.real_tools import RealTools
        tools = RealTools("same-trace")
        tools.calls, tools.http_requests = 2, 2
        prior = {"call_id": "same-trace:tool:2", "status": "error",
                 "http_requests": 1, "attempt": 2, "retry_of": "same-trace:tool:1"}
        tools.history[prior["call_id"]] = prior
        tools._transport_attempts["query"] = prior
        restored = RealTools("same-trace")
        restored.restore(json.loads(json.dumps(tools.snapshot())))
        self.assertEqual((restored.calls, restored.http_requests), (2, 2))
        self.assertEqual(restored._transport_attempts["query"]["retry_of"], "same-trace:tool:1")
        restored.history.clear()
        self.assertEqual(len(tools.history), 1)

    def test_restored_wall_duration_includes_offline_time(self):
        with patch("travel_agent.agent.time.time", return_value=100):
            execution = TripExecution(self.scenario, Baseline(), self.store)
        state = execution.snapshot()
        restored = TripExecution.restore(state, Baseline(), self.store)
        with patch("travel_agent.agent.time.time", return_value=1110):
            result = restored.finalize()
        self.assertEqual(result["duration_ms"], 1010000)

    def test_real_policy_source_is_preserved_and_changes_rejected(self):
        path = self.store.path.parent / "custom.md"
        path.write_text("custom policy", encoding="utf-8")
        execution = TripExecution(self.scenario, Baseline(), self.store, tool_mode="real", policy_path=path)
        state = execution.snapshot()
        restored = TripExecution.restore(state, Baseline(), self.store)
        self.assertEqual(restored.toolbox.policy_path, path.resolve())
        path.write_text("changed", encoding="utf-8")
        with self.assertRaises(ValueError):
            TripExecution.restore(state, Baseline(), self.store)

    def test_provider_endpoint_cannot_change_or_leak_credentials(self):
        provider = Baseline()
        provider.base_url = "http://localhost:11434"
        execution = TripExecution(self.scenario, provider, self.store)
        state = execution.snapshot()
        provider.base_url = "http://localhost:9999"
        with self.assertRaises(ValueError):
            TripExecution.restore(state, provider, self.store)
        provider.base_url = "http://user:secret@localhost:11434"
        with self.assertRaises(ValueError):
            execution.snapshot()
