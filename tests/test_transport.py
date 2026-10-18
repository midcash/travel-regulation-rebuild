import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from travel_agent.agent import plan_trip
from travel_agent.providers import tool_call
from travel_agent.telemetry import Store


REQUEST = {"origin": "杭州", "destination": "三亚", "meeting_location": "三亚湾红树林国际会议中心",
           "meeting_at": "2026-09-19T09:00+08:00", "transport_mode": "flight", "arrive_previous_day": True}


class TransportModel:
    name, model = "offline", "transport-contract"

    def chat(self, messages, schemas, timeout):
        self.request = json.loads(messages[1]["content"])
        results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        if not results:
            message = tool_call("search_transport", {"mode": "flight", "origin": "杭州",
                "destination": "三亚", "departure_date": "2026-09-18"})
        else:
            message = tool_call("finish", {"status": "needs_review", "reason": "missing_data",
                "segment_ids": [], "evidence_ids": results[-1]["evidence_refs"]})
        return {"message": message, "input_tokens": 0, "output_tokens": 0}


class RequeryModel(TransportModel):
    def __init__(self, queries):
        self.queries = queries

    def chat(self, messages, schemas, timeout):
        count = sum(m["role"] == "tool" for m in messages)
        if count < len(self.queries):
            args = {"mode": "flight", "origin": "杭州", "destination": "三亚",
                    "departure_date": "2026-09-18", **self.queries[count]}
            return {"message": tool_call("search_transport", args), "input_tokens": 0, "output_tokens": 0}
        return super().chat(messages, schemas, timeout)


class TransportTests(unittest.TestCase):
    def test_afternoon_flight_is_retained_with_request_and_evidence(self):
        def supplier(request):
            body = json.loads(request.content)
            self.assertEqual(body["params"]["name"], "searchLowestPriceFlight")
            self.assertEqual(body["params"]["arguments"], {"departureCityName": "杭州",
                "arrivalCityName": "三亚", "departureDate": "2026-09-18"})
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {
                "structuredContent": {"data": [{"flightNumber": "HU7386",
                    "departureTime": "2026-09-18 16:05:00", "arrivalTime": "2026-09-18 18:55:00",
                    "departureAirport": "萧山机场T3", "arrivalAirport": "凤凰机场T3"}]}}})
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"TUNIU_API_KEY": "test-secret"}):
            model = TransportModel()
            run = plan_trip({"id": "transport-contract", "request": REQUEST}, model,
                Store(Path(directory) / "trace.sqlite3"), tool_mode="real",
                transport_http=httpx.MockTransport(supplier))
            self.assertEqual(run["termination"], "completed")
            self.assertEqual(model.request["transport_mode"], "flight")
            self.assertEqual(model.request["required_arrival_date"], "2026-09-18")
            self.assertNotIn("earliest_departure", model.request)
            result = run["tool_results"][0]
            self.assertEqual(result["status"], "ok")
            candidate = result["data"]["candidates"][0]
            self.assertTrue(candidate["meets_request"])
            self.assertEqual(candidate["departure_at"], "2026-09-18 16:05:00")
            self.assertEqual(candidate["arrival_at"], "2026-09-18 18:55:00")
            self.assertIn(candidate["evidence_ref"], run["observed_evidence_ids"])
            self.assertIsNone(candidate["price_cny"])
            self.assertEqual(run["verdict"]["reason"], "missing_data")
            self.assertEqual(run["http_requests"], 1)

    def run_supplier(self, supplier, model=None, request=None, limits=None):
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"TUNIU_API_KEY": "test-secret"}):
            store = Store(Path(directory) / "trace.sqlite3")
            run = plan_trip({"id": "transport-contract", "request": request or REQUEST},
                model or TransportModel(), store, limits=limits, tool_mode="real",
                transport_http=httpx.MockTransport(supplier))
            persisted = store.get_run(run["trace_id"])
            files = [p.read_text(encoding="utf-8") for p in Path(directory).rglob("transport-*.json")]
            return run, persisted, files

    @staticmethod
    def response(request, rows):
        body = json.loads(request.content)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {
            "content": [{"type": "text", "text": json.dumps({"data": rows})}]}})

    def test_arrival_date_and_supplier_times_are_checked_without_rewriting(self):
        for arrival, departure, expected in [
            ("2026-09-19 00:10:00", "2026-09-18 21:00:00", "not_previous_day_arrival"),
            ("00:10", "2026-09-18 21:00:00", "unknown_or_invalid_times"),
            ("2026-09-18 18:55:00", "2026-09-18 19:00:00", "reversed_times"),
            ("2026-09-18 18:55:00", "2026-09-17 16:05:00", "wrong_departure_date"),
            ("2026-09-19T01:00:00Z", "2026-09-18T16:05:00+08:00", "not_previous_day_arrival")]:
            with self.subTest(arrival=arrival, departure=departure):
                run, _, _ = self.run_supplier(lambda req: self.response(req, [{"flightNumber": "TEST",
                    "departureTime": departure, "arrivalTime": arrival}]))
                self.assertEqual(run["termination"], "completed")
                candidate = run["tool_results"][0]["data"]["candidates"][0]
                self.assertFalse(candidate["meets_request"])
                self.assertIn(expected, candidate["constraint_reasons"])
                self.assertEqual(candidate["arrival_at"], arrival)
                self.assertEqual(candidate["departure_at"], departure)
                self.assertEqual(run["status"], "needs_review")

    def test_supplier_failures_are_explicit_and_never_fabricate_candidates(self):
        def timeout(request):
            raise httpx.ReadTimeout("secret URL test-secret", request=request)
        def malformed(request):
            return httpx.Response(200, content=b"{bad")
        def denied(request):
            return httpx.Response(401, text="test-secret")
        def business_error(request):
            body = json.loads(request.content)
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {
                "structuredContent": {"successCode": False, "data": None}}})
        for supplier, expected, retryable in [(timeout, "TransportTimeout", True),
                (malformed, "SupplierResponseError", False), (denied, "SupplierHTTPError", False),
                (business_error, "SupplierResponseError", False)]:
            with self.subTest(error=expected):
                run, trace, files = self.run_supplier(supplier)
                self.assertEqual(run["termination"], "completed")
                result = run["tool_results"][0]
                self.assertEqual(result["status"], "error")
                self.assertIsNone(result["data"])
                self.assertEqual(result["error"]["type"], expected)
                self.assertEqual(result["error"]["retryable"], retryable)
                self.assertEqual(run["http_requests"], 1)
                self.assertEqual(len(files), 1)
                self.assertNotIn("test-secret", json.dumps(trace) + "".join(files))

    def test_whole_request_timeout_includes_waiting_for_response(self):
        import asyncio
        from travel_agent.agent import Limits
        async def slow(request):
            await asyncio.sleep(1)
            return self.response(request, [])
        run, _, _ = self.run_supplier(slow, limits=Limits(timeout_s=0.08))
        self.assertEqual(run["termination"], "budget_exceeded")
        self.assertEqual(run["budget_reason"], "deadline")
        self.assertEqual(run["tool_results"][0]["error"]["type"], "TransportTimeout")
        self.assertLess(run["duration_ms"], 700)

    def test_sse_success_and_decimal_values_are_redacted_and_preserved(self):
        def supplier(request):
            rpc_id = json.loads(request.content)["id"]
            payload = '{"data":[{"flightNumber":"TEST", "airlineCompany":"test-secret", "basePrice":123.45,"totalTax":50.01,"departureTime":"2026-09-18 16:05:00","arrivalTime":"2026-09-18 18:55:00"}]}'
            envelope = {"jsonrpc": "2.0", "id": rpc_id, "result": {"content": [{"type": "text", "text": payload}]}}
            content = 'data: {"notification":true}\n\ndata: ' + json.dumps(envelope) + '\n\ndata: unfinished'
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=content)
        run, trace, files = self.run_supplier(supplier)
        self.assertEqual(run["termination"], "completed")
        candidate = run["tool_results"][0]["data"]["candidates"][0]
        self.assertEqual(candidate["price_cny"], "173.46")
        self.assertTrue(candidate["meets_request"])
        self.assertNotIn("test-secret", json.dumps(trace) + "".join(files))
        self.assertIn("REDACTED", candidate["operator"])

    def test_train_search_remains_available_without_overriding_flight_preference(self):
        class Train(TransportModel):
            def chat(self, messages, schemas, timeout):
                response = super().chat(messages, schemas, timeout)
                function = response["message"]["tool_calls"][0]["function"]
                if function["name"] == "search_transport":
                    function["arguments"]["mode"] = "train"
                return response
        def supplier(request):
            self.assertEqual(json.loads(request.content)["params"]["name"], "searchLowestPriceTrain")
            return self.response(request, [{"trainNum": "G1", "departStationName": "杭州东",
                "destStationName": "三亚", "departureTime": "2026-09-18 06:00:00",
                "arrivalTime": "2026-09-18 22:00:00", "price": {"二等座": "888.50"}}])
        for mode, expected in [("train", True), ("flight", False)]:
            run, _, _ = self.run_supplier(supplier, Train(), {**REQUEST, "transport_mode": mode})
            self.assertEqual(run["termination"], "completed")
            candidate = run["tool_results"][0]["data"]["candidates"][0]
            self.assertEqual(candidate["meets_request"], expected)
            self.assertEqual(candidate["train_no"], "G1")
            self.assertEqual(candidate["lowest_price_cny"], "888.50")

    def test_empty_success_is_distinct_from_failure(self):
        run, _, _ = self.run_supplier(lambda req: self.response(req, []))
        self.assertEqual(run["tool_results"][0]["status"], "ok")
        self.assertEqual(run["tool_results"][0]["data"]["candidates"], [])
        self.assertEqual(run["verdict"]["reason"], "missing_data")

    def test_evidence_write_failure_preserves_supplier_result_and_request_count(self):
        def supplier(request):
            return self.response(request, [{"flightNumber": "HU7386",
                "departureTime": "2026-09-18 16:05:00", "arrivalTime": "2026-09-18 18:55:00"}])
        with patch.object(Path, "write_text", side_effect=PermissionError("test-secret write failed")):
            run, trace, files = self.run_supplier(supplier)
        self.assertEqual(run["termination"], "completed")
        self.assertEqual(run["http_requests"], 1)
        self.assertEqual(run["retries"], 0)
        result = run["tool_results"][0]
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["data"]["candidates"][0]["flight_no"], "HU7386")
        self.assertEqual(result["evidence_error"]["type"], "EvidenceWriteError")
        self.assertFalse(result["evidence_error"]["retryable"])
        self.assertTrue(result["warnings"])
        self.assertEqual(run["observed_evidence_ids"], result["evidence_refs"])
        self.assertEqual(files, [])
        self.assertNotIn("source_ref", run["evidence"][0]["supplier_evidence"])
        self.assertEqual(trace["tool_results"], run["tool_results"])
        span = next(s for s in trace["spans"] if s["name"] == "execute_tool")
        self.assertEqual(span["attributes"]["http_requests"], 1)
        self.assertEqual(span["attributes"]["evidence_error.type"], "EvidenceWriteError")
        self.assertNotIn("test-secret", json.dumps(trace))

    def test_missing_key_never_contacts_supplier(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"TUNIU_API_KEY": ""}):
            run = plan_trip({"id": "missing-key", "request": REQUEST}, RequeryModel([{}, {}]),
                Store(Path(directory) / "trace.sqlite3"), tool_mode="real",
                transport_http=httpx.MockTransport(lambda req: self.fail("Unexpected HTTP request")))
            self.assertEqual(run["termination"], "completed")
            self.assertEqual(run["http_requests"], 0)
            self.assertEqual(run["tool_calls"], 2)
            self.assertEqual(run["retries"], 0)
            self.assertEqual(run["tool_results"][0]["error"]["type"], "ConfigurationError")
            self.assertEqual(run["evidence"], [])

    def test_model_retries_are_counted_and_linked_to_failed_attempts(self):
        requests = []
        def supplier(request):
            requests.append(json.loads(request.content)["params"])
            if len(requests) < 3:
                raise httpx.ReadTimeout("test-secret timeout", request=request)
            return self.response(request, [])
        model = RequeryModel([{}, {"service_class": "any", "max_results": 5}, {}])
        run, trace, files = self.run_supplier(supplier, model)
        self.assertEqual(run["termination"], "completed")
        self.assertEqual(run["retries"], 2)
        self.assertEqual(run["http_requests"], 3)
        self.assertEqual(run["tool_calls"], 3)
        self.assertEqual(len(requests), 3)
        self.assertEqual(requests[0], requests[1])
        self.assertEqual(requests[1], requests[2])
        results = run["tool_results"]
        self.assertEqual([r["status"] for r in results], ["error", "error", "ok"])
        self.assertEqual([r["attempt"] for r in results], [1, 2, 3])
        self.assertEqual([r["retry_of"] for r in results], [None, results[0]["call_id"], results[1]["call_id"]])
        spans = [s for s in trace["spans"] if s["name"] == "execute_tool"]
        self.assertEqual([s["attributes"]["attempt"] for s in spans], [1, 2, 3])
        self.assertEqual(spans[-1]["attributes"]["retry_of"], results[1]["call_id"])
        self.assertEqual(len(set(run["observed_evidence_ids"])), 3)
        self.assertEqual(len(files), 3)

    def test_changed_query_and_successful_refresh_are_not_retries(self):
        requests = []
        def supplier(request):
            requests.append(json.loads(request.content)["params"]["arguments"])
            if len(requests) == 1:
                raise httpx.ReadTimeout("offline timeout", request=request)
            return self.response(request, [])
        model = RequeryModel([{}, {"departure_date": "2026-09-17"}, {}, {}])
        run, _, _ = self.run_supplier(supplier, model)
        self.assertEqual(run["termination"], "completed")
        self.assertEqual(run["http_requests"], 4)
        self.assertEqual(run["tool_calls"], 4)
        self.assertEqual(run["retries"], 1)
        self.assertEqual(requests[1]["departureDate"], "2026-09-17")
        results = run["tool_results"]
        self.assertEqual([r["attempt"] for r in results], [1, 1, 2, 1])
        self.assertEqual([r["retry_of"] for r in results], [None, None, results[0]["call_id"], None])

    def test_retry_count_stops_at_actual_tool_budget(self):
        from travel_agent.agent import Limits
        requests = []
        def supplier(request):
            requests.append(request)
            raise httpx.ReadTimeout("offline timeout", request=request)
        run, trace, _ = self.run_supplier(supplier, RequeryModel([{}, {}, {}]),
                                         limits=Limits(max_tool_calls=2))
        self.assertEqual(run["termination"], "budget_exceeded")
        self.assertEqual(run["budget_reason"], "tool_calls")
        self.assertEqual(len(requests), 2)
        self.assertEqual(run["http_requests"], 2)
        self.assertEqual(run["tool_calls"], 2)
        self.assertEqual(run["retries"], 1)
        self.assertEqual([r["attempt"] for r in trace["tool_results"]], [1, 2])
        self.assertTrue(all(r["data"] is None for r in trace["tool_results"]))

    def test_supplier_failure_and_evidence_failure_are_both_preserved(self):
        def supplier(request):
            raise httpx.ReadTimeout("test-secret timeout", request=request)
        with patch.object(Path, "write_text", side_effect=OSError("test-secret disk full")):
            run, trace, _ = self.run_supplier(supplier, RequeryModel([{}, {}]))
        self.assertEqual(run["termination"], "completed")
        self.assertEqual(run["http_requests"], 2)
        self.assertEqual(run["retries"], 1)
        self.assertEqual(len(trace["evidence"]), 2)
        for result in trace["tool_results"]:
            self.assertEqual(result["status"], "error")
            self.assertIsNone(result["data"])
            self.assertEqual(result["error"]["type"], "TransportTimeout")
            self.assertTrue(result["error"]["retryable"])
            self.assertEqual(result["evidence_error"]["type"], "EvidenceWriteError")
        self.assertNotIn("test-secret", json.dumps(trace))

    def test_evidence_directory_failure_preserves_result(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"TUNIU_API_KEY": "test-secret"}):
            store = Store(Path(directory) / "trace.sqlite3")
            (Path(directory) / "evidence").write_text("occupied", encoding="utf-8")
            run = plan_trip({"id": "directory-error", "request": REQUEST}, TransportModel(),
                store, tool_mode="real", transport_http=httpx.MockTransport(lambda req: self.response(req, [])))
            self.assertEqual(run["termination"], "completed")
            self.assertEqual(run["http_requests"], 1)
            result = run["tool_results"][0]
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["evidence_error"]["type"], "EvidenceWriteError")
            self.assertNotIn("source_ref", run["evidence"][0]["supplier_evidence"])
