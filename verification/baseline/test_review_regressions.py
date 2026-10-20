"""Cubic regressions: offline acceptance criteria, provenance and decorative UI."""
import asyncio
from contextlib import redirect_stdout
from datetime import timedelta
from html.parser import HTMLParser
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx

import live_smoke
from fixtures import NOW, supplier
from travel_agent.runtime import Conversation, FALLBACK_REPLIES, NO_ANSWER_TEXT, NodeWorker

ROOT = Path(__file__).resolve().parents[2]
ARGS = {"origin": "杭州", "destination": "三亚", "departure_date": "2026-11-20"}
RESULT = {"status": "ok", "data": {"candidates": [{"flight_no": "SYNTHETIC"}]}}


class SmokeCriteria(unittest.IsolatedAsyncioTestCase):
    async def run_smoke(self, turns):
        class StubConversation:
            def __init__(self):
                self.messages = []
                self.worker = SimpleNamespace(flight_requests=0)
                self.tools = SimpleNamespace(calls=0)
                self.submitted = 0
                self.close = AsyncMock()

            def submit(self, prompt):
                state = turns[self.submitted]
                self.submitted += 1
                self.run = {"status": state.get("status", "complete"), "results": state.get("results", [])}
                self.messages.append({"text": state.get("text", "请补充出发地和日期。")})
                self.worker.flight_requests = state.get("requests", 0)
                self.tools.calls = state.get("calls", 0)
                self.task = asyncio.create_task(asyncio.sleep(0))

        conversation = StubConversation()
        with patch.object(live_smoke, "load_env"), patch.object(live_smoke, "Conversation", return_value=conversation), redirect_stdout(io.StringIO()):
            code = await live_smoke.main()
        conversation.close.assert_awaited_once()
        return code, conversation.submitted

    async def test_rejects_first_turn_tool_attempt_or_results_without_spending_a_second_turn(self):
        for first in ({"requests": 1}, {"calls": 1}, {"results": [RESULT]}, {"text": NO_ANSWER_TEXT}):
            with self.subTest(first=first):
                code, submitted = await self.run_smoke([first])
                self.assertNotEqual(code, 0)
                self.assertEqual(submitted, 1)

    async def test_rejects_missing_query_bad_status_empty_or_fallback_final_answer(self):
        good = {"requests": 1, "calls": 1, "results": [RESULT], "text": "已取得航班查询结果。"}
        changes = [{"results": []}, {"results": [{"status": "ok", "data": {"candidates": []}}]},
                   {"results": [{"status": "ok", "data": None}]},
                   {"status": "supplier_error"}, {"requests": 0}, {"calls": 0}, {"text": " "},
                   {"text": NO_ANSWER_TEXT}, *({"text": text} for text in FALLBACK_REPLIES.values())]
        for change in changes:
            with self.subTest(change=change):
                code, submitted = await self.run_smoke([{}, {**good, **change}])
                self.assertNotEqual(code, 0)
                self.assertEqual(submitted, 2)

    async def test_accepts_two_turn_success_without_adding_calls(self):
        code, submitted = await self.run_smoke([{}, {"requests": 1, "calls": 1, "results": [RESULT], "text": "查询完成，见航班卡片。"}])
        self.assertEqual((code, submitted), (0, 2))

    async def test_counts_flight_bridge_requests_even_before_python_execution(self):
        worker = live_smoke.ObservedWorker()
        events = [{"type": "tool_request", "name": "search_flights"}, {"type": "model_start"}]
        with patch.object(NodeWorker, "receive", new=AsyncMock(side_effect=events)):
            await worker.receive()
            await worker.receive()
        self.assertEqual(worker.flight_requests, 1)


class Provenance(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.env = patch.dict(os.environ, {"TUNIU_API_KEY": "synthetic-secret", "TUNIU_FLIGHT_URL": "https://supplier.invalid/flight"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.requests = []
        self.handler = supplier
        self.tick = NOW

        def http(request):
            self.requests.append(request)
            self.tick += timedelta(seconds=3)
            return self.handler(request)

        self.conversation = Conversation(clock=lambda: self.tick, transport_http=httpx.MockTransport(http), evidence_root=folder.name)

    def query(self, args=None):
        raw = self.conversation._execute({"name": "search_flights", "args": args or ARGS}, NOW, 45)
        return self.conversation._public_result(raw)

    def assert_local(self, result, kind):
        self.assertEqual(result["failure_kind"], kind)
        self.assertEqual(result["evidence_refs"], [])
        for key in ("source", "attempted_at", "observed_at"):
            self.assertNotIn(key, result)
        self.assertEqual(self.requests, [])

    def test_validation_and_tool_errors_never_claim_supplier_observations(self):
        self.assert_local(self.query({**ARGS, "departure_date": "2026-10-18"}), "invalid_query")
        for exception in (RuntimeError, ValueError, TypeError, KeyError):
            with self.subTest(exception=exception), patch.object(self.conversation.tools, "execute", side_effect=exception("private internal failure")):
                result = self.query()
            self.assert_local(result, "tool_error")
            self.assertNotIn("private internal failure", json.dumps(result))

    def test_missing_supplier_configuration_is_local_not_an_outage(self):
        with patch.dict(os.environ, {"TUNIU_API_KEY": ""}):
            self.assert_local(self.query(), "configuration_error")

    def test_success_preserves_evidence_and_distinct_attempt_and_observation_times(self):
        result = self.query()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["source"], "途牛航班接口")
        self.assertEqual(result["attempted_at"], NOW.isoformat())
        self.assertEqual(result["observed_at"], (NOW + timedelta(seconds=3)).isoformat())
        self.assertTrue(result["evidence_refs"])
        self.assertEqual(result["data"]["candidates"][0]["evidence_ref"], result["evidence_refs"][0])
        self.assertNotIn("wire_text", json.dumps(result))
        self.assertNotIn("synthetic-secret", json.dumps(result))

    def test_failed_real_attempt_keeps_request_evidence_without_success_observation(self):
        for failure in ("http", "timeout", "network", "response"):
            with self.subTest(failure=failure):
                def fail(request):
                    if failure == "timeout":
                        raise httpx.ReadTimeout("synthetic timeout", request=request)
                    if failure == "network":
                        raise httpx.ConnectError("synthetic network error", request=request)
                    if failure == "response":
                        return httpx.Response(200, json={"invalid": "envelope"})
                    return httpx.Response(503)
                self.handler = fail
                result = self.query()
                self.assertEqual(result["failure_kind"], "supplier_error")
                self.assertEqual(result["http_requests"], 1)
                self.assertTrue(result["evidence_refs"])
                self.assertIn("source", result)
                self.assertIn("attempted_at", result)
                self.assertNotIn("observed_at", result)

    def test_success_without_candidates_still_has_a_real_observation(self):
        self.handler = lambda r: httpx.Response(200, json={"jsonrpc": "2.0", "id": json.loads(r.content)["id"],
            "result": {"structuredContent": {"data": []}}})
        result = self.query()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["data"]["candidates"], [])
        self.assertIn("observed_at", result)


class WebSemantics(unittest.TestCase):
    def test_visual_rail_is_decorative_and_has_no_fake_navigation(self):
        class RailParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.depth = 0
                self.rail = None
                self.children = []

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if "rail" in attrs.get("class", "").split():
                    self.rail = (tag, attrs)
                    self.depth = 1
                elif self.depth:
                    self.depth += 1
                    self.children.append((tag, attrs))

            def handle_endtag(self, tag):
                if self.depth:
                    self.depth -= 1

        parser = RailParser()
        parser.feed((ROOT / "travel_agent/web/index.html").read_text(encoding="utf-8"))
        tag, attrs = parser.rail
        self.assertNotEqual(tag, "nav")
        self.assertNotEqual(attrs.get("role"), "navigation")
        self.assertEqual(attrs.get("aria-hidden"), "true")
        for tag, attrs in parser.children:
            self.assertNotIn(tag, {"a", "button", "input"})
            self.assertNotIn("tabindex", attrs)
            self.assertNotIn("aria-label", attrs)

    def test_result_renderer_handles_local_and_supplier_failures_without_invented_metadata(self):
        result = subprocess.run(["node", str(Path(__file__).with_name("web_review_checks.mjs"))],
                                cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("render checks passed", result.stdout)
