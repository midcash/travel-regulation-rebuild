"""Black-box contracts at RealTools; never call the old Agent or HIL."""
from datetime import date
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from travel_agent.real_tools import REAL_SCHEMAS, RealTools
from suppliers import FLIGHT, REQUEST, STAY, rpc, supplier


class ToolsCase(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.env = patch.dict("os.environ", {"TUNIU_API_KEY": "test-secret", "AMAP_API_KEY": "amap-secret",
            "TUNIU_FLIGHT_URL": "https://supplier.invalid/flight", "TUNIU_TRAIN_URL": "https://supplier.invalid/train",
            "TUNIU_HOTEL_URL": "https://supplier.invalid/hotel"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.requests = []
        self.handler = supplier

        def http(request):
            self.requests.append(request)
            return self.handler(request)

        self.tools = RealTools("contract", request=dict(REQUEST), current_date=date(2099, 9, 1),
            evidence_dir=self.folder.name, transport_http=httpx.MockTransport(http))

    def call(self, name, args):
        return self.tools.execute(name, args)

    def candidate(self, name="search_transport", args=None):
        return self.call(name, args or FLIGHT)["data"]["candidates"][0]["candidate_id"]


class ToolsContract(ToolsCase):
    def test_registry_exposes_only_six_supplier_tools_and_rejects_removed_tools(self):
        self.assertEqual({s["function"]["name"] for s in REAL_SCHEMAS}, {
            "search_transport", "get_transport_detail", "search_stays",
            "get_stay_detail", "resolve_place", "calculate_route"})
        for name in ("finish", "get_policy_snapshot"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.call(name, {})
        self.assertEqual(self.requests, [])

    def test_flight_search_preserves_facts_unknown_price_and_evidence(self):
        result = self.call("search_transport", FLIGHT)
        self.assertEqual(result["status"], "ok")
        row = result["data"]["candidates"][0]
        self.assertEqual(row["arrival_at"], "2099-09-18 18:55:00")
        self.assertTrue(row["meets_request"])
        self.assertIsNone(row["price_cny"])
        self.assertIn(row["evidence_ref"], self.tools.seen)
        self.assertEqual(json.loads(self.requests[0].content)["params"]["arguments"], {
            "departureCityName": "杭州", "arrivalCityName": "三亚", "departureDate": "2099-09-18"})

    def test_train_search_and_detail_preserve_station_context_and_precision(self):
        self.tools.request["transport_mode"] = "train"
        result = self.call("search_transport", {**FLIGHT, "mode": "train"})
        row = result["data"]["candidates"][0]
        self.assertTrue(row["meets_request"])
        self.assertEqual(row["lowest_price_cny"], "888.50")
        detail = self.call("get_transport_detail", {"candidate_id": row["candidate_id"]})
        self.assertEqual(detail["status"], "ok")
        seat = detail["data"]["seat_options"][0]
        self.assertEqual(seat["price"], "123.45010001")
        self.assertIsNone(seat["leftNumber"])
        self.assertEqual(json.loads(self.requests[-1].content)["params"]["arguments"], {
            "departureStationName": "杭州东", "arrivalStationName": "三亚", "departureDate": "2099-09-18", "trainNum": "G123"})

    def test_flight_detail_uses_observed_identity_and_links_both_queries(self):
        identifier = self.candidate()
        detail = self.call("get_transport_detail", {"candidate_id": identifier})
        self.assertEqual(detail["status"], "ok")
        self.assertEqual(detail["data"]["flight"]["arrivalTerminal"], "T1")
        quote = detail["data"]["cabin_options"][0]
        self.assertEqual(quote["basePrice"], "123.45010001")
        self.assertIsNone(quote["totalTax"])
        self.assertIn(detail["data"]["candidate_evidence_ref"], self.tools.seen)
        self.assertNotEqual(detail["evidence_refs"], self.tools.history["contract:tool:1"]["evidence_refs"])

    def test_foreign_or_overridden_detail_makes_no_http(self):
        for name in ("get_transport_detail", "get_stay_detail"):
            for args in ({"candidate_id": "foreign"}, {"candidate_id": True},
                         {"candidate_id": "foreign", "departure_date": "2099-09-17"}):
                with self.subTest(name=name, args=args), self.assertRaises(ValueError):
                    self.call(name, args)
        self.assertEqual(self.requests, [])

    def test_wrong_flight_identity_or_date_and_bad_shapes_keep_error_evidence(self):
        identifier = self.candidate()
        for payload in ({"flightInfo": {"flightNo": "OTHER"}, "cabinInfo": []},
                        {"flightInfo": {"flightNo": "HU1", "departureDate": "2099-09-17"}, "cabinInfo": []},
                        {"flightInfo": {}, "cabinInfo": {}}, {"data": []}):
            with self.subTest(payload=payload):
                self.handler = lambda req: rpc(req, payload)
                result = self.call("get_transport_detail", {"candidate_id": identifier})
                self.assertEqual(result["status"], "error")
                self.assertIsNone(result["data"])
                self.assertTrue(result["evidence_refs"])

    def test_multi_night_stay_and_detail_keep_dates_unknown_basis_and_raw_rates(self):
        identifier = self.candidate("search_stays", STAY)
        result = self.call("get_stay_detail", {"candidate_id": identifier})
        self.assertEqual(result["status"], "ok")
        self.assertEqual((result["data"]["check_in"], result["data"]["check_out"]), ("2099-09-18", "2099-09-21"))
        self.assertEqual(result["data"]["price_basis"], "unknown")
        self.assertEqual(result["data"]["room_types"][0]["ratePlans"][0]["rmbPrices"], "321.45010001")
        self.assertEqual(json.loads(self.requests[-1].content)["params"]["arguments"], {
            "hotelId": 34548, "checkIn": "2099-09-18", "checkOut": "2099-09-21", "roomNum": 1, "adultNum": 1})

    def test_invalid_stay_dates_make_no_http(self):
        for start, end in (("bad", "2099-09-21"), ("2099-09-21", "2099-09-21"), ("2099-09-22", "2099-09-21")):
            with self.subTest(start=start), self.assertRaises(ValueError):
                self.call("search_stays", {**STAY, "check_in": start, "check_out": end})
        self.assertEqual(self.requests, [])

    def test_wrong_hotel_identity_dates_and_room_shapes_are_errors(self):
        identifier = self.candidate("search_stays", STAY)
        for payload in ({"hotelId": 99, "roomTypes": []}, {"roomTypes": {}},
                        {"roomTypes": [{"ratePlans": {}}]}, {"roomTypes": [None]},
                        {"roomTypes": [], "checkIn": "2099-09-17"}, {"roomTypes": [], "checkOut": "2099-09-20"}):
            with self.subTest(payload=payload):
                self.handler = lambda req: rpc(req, payload)
                result = self.call("get_stay_detail", {"candidate_id": identifier})
                self.assertEqual(result["status"], "error")
                self.assertIsNone(result["data"])
                self.assertTrue(result["evidence_refs"])

    def test_missing_candidate_identity_never_fabricates_detail(self):
        self.handler = lambda req: rpc(req, {"hotels": [{"hotelName": "无ID酒店"}]})
        identifier = self.candidate("search_stays", STAY)
        result = self.call("get_stay_detail", {"candidate_id": identifier})
        self.assertEqual(result["error"]["type"], "MissingCandidateData")
        self.assertEqual(len(self.requests), 1)

    def test_past_dates_are_rejected_before_network(self):
        for name, args in (("search_transport", {**FLIGHT, "departure_date": "2099-08-31"}),
                           ("search_stays", {**STAY, "check_in": "2099-08-31"})):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.call(name, args)
        self.assertEqual(self.requests, [])

    def test_supplier_failures_are_distinct_and_redacted(self):
        def timeout(req):
            raise httpx.ReadTimeout("test-secret", request=req)
        for handler, kind, retryable in ((timeout, "TransportTimeout", True),
                (lambda _: httpx.Response(401, text="test-secret"), "SupplierHTTPError", False),
                (lambda _: httpx.Response(200, content=b"{bad"), "SupplierResponseError", False),
                (lambda req: rpc(req, {"successCode": False, "data": None}), "SupplierResponseError", False)):
            with self.subTest(kind=kind):
                self.handler = handler
                result = self.call("search_transport", FLIGHT)
                self.assertEqual(result["status"], "error")
                self.assertEqual((result["error"]["type"], result["error"]["retryable"]), (kind, retryable))
                self.assertIsNone(result["data"])
                self.assertEqual(result["http_requests"], 1)
        text = json.dumps(self.tools.snapshot()) + "".join(p.read_text(encoding="utf-8") for p in Path(self.folder.name).glob("*.json"))
        self.assertNotIn("test-secret", text)

    def test_missing_key_and_bad_endpoint_never_contact_supplier(self):
        for values in ({"TUNIU_API_KEY": ""}, {"TUNIU_FLIGHT_URL": "http://unsafe.invalid"}):
            with self.subTest(values=values), patch.dict("os.environ", values):
                result = self.call("search_transport", FLIGHT)
                self.assertEqual(result["error"]["type"], "ConfigurationError")
                self.assertEqual(result["http_requests"], 0)
        self.assertEqual(self.requests, [])

    def test_empty_success_is_distinct_from_failure(self):
        self.handler = lambda req: rpc(req, {"data": []})
        result = self.call("search_transport", FLIGHT)
        self.assertEqual((result["status"], result["data"]["candidates"]), ("ok", []))

    def test_sse_response_preserves_decimal_money_and_redacts_supplier_text(self):
        def sse(req):
            payload = '{"data":[{"flightNumber":"HU1","airlineCompany":"test-secret","basePrice":123.45,"totalTax":50.01,"departureTime":"2099-09-18 16:05:00","arrivalTime":"2099-09-18 18:55:00"}]}'
            envelope = {"jsonrpc": "2.0", "id": json.loads(req.content)["id"], "result": {
                "content": [{"type": "text", "text": payload}]}}
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                content='data: {"notification":true}\n\ndata: ' + json.dumps(envelope) + '\n\ndata: unfinished')
        self.handler = sse
        result = self.call("search_transport", FLIGHT)
        self.assertEqual(result["status"], "ok")
        row = result["data"]["candidates"][0]
        self.assertEqual(row["price_cny"], "173.46")
        self.assertIn("REDACTED", row["operator"])
        self.assertNotIn("test-secret", json.dumps(self.tools.snapshot()))

    def test_high_speed_filter_happens_before_result_limit(self):
        rows = [{"trainNum": "K" + str(i)} for i in range(6)] + [{"trainNum": "G123"}]
        self.handler = lambda req: rpc(req, {"data": rows})
        result = self.call("search_transport", {**FLIGHT, "mode": "train", "service_class": "high_speed_rail", "max_results": 1})
        self.assertEqual(result["status"], "ok")
        self.assertEqual([c["train_no"] for c in result["data"]["candidates"]], ["G123"])

    def test_invalid_supplier_times_are_retained_but_flagged(self):
        for departure, arrival, reason in (("2099-09-18 19:00:00", "2099-09-18 18:55:00", "reversed_times"),
                ("2099-09-17 16:05:00", "2099-09-18 18:55:00", "wrong_departure_date"),
                ("2099-09-18 16:05:00", "00:10", "unknown_or_invalid_times")):
            with self.subTest(reason=reason):
                self.handler = lambda req: rpc(req, {"data": [{"flightNumber": "HU1", "departureTime": departure, "arrivalTime": arrival}]})
                result = self.call("search_transport", FLIGHT)
                row = result["data"]["candidates"][0]
                self.assertFalse(row["meets_request"])
                self.assertIn(reason, row["constraint_reasons"])
                self.assertEqual(row["arrival_at"], arrival)

    def test_oversized_response_is_structured_failure(self):
        self.handler = lambda req: httpx.Response(200, content=b"x" * 65)
        with patch("travel_agent.transport.MAX_BYTES", 64):
            result = self.call("search_transport", FLIGHT)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["http_requests"], 1)
        self.assertIsNone(result["data"])

    def test_timeout_includes_response_wait(self):
        import asyncio
        import time
        async def slow(req):
            await asyncio.sleep(1)
            return rpc(req, {"data": []})
        self.tools.transport_http = httpx.MockTransport(slow)
        start = time.monotonic()
        result = self.tools.execute("search_transport", FLIGHT, timeout=0.05)
        self.assertEqual(result["error"]["type"], "TransportTimeout")
        self.assertLess(time.monotonic() - start, 0.8)

    def test_evidence_write_failure_keeps_supplier_result_and_counts(self):
        with patch.object(Path, "write_text", side_effect=PermissionError("test-secret")):
            result = self.call("search_transport", FLIGHT)
        self.assertEqual((result["status"], result["http_requests"]), ("ok", 1))
        self.assertEqual(result["evidence_error"]["type"], "EvidenceWriteError")
        self.assertTrue(result["warnings"])
        self.assertNotIn("source_ref", next(iter(self.tools.seen.values()))["supplier_evidence"])

    def test_retry_history_tracks_actual_query_and_successful_refresh(self):
        def timeout(req):
            raise httpx.ReadTimeout("offline", request=req)
        self.handler = timeout
        first = self.call("search_transport", FLIGHT)
        other = self.call("search_transport", {**FLIGHT, "departure_date": "2099-09-17"})
        retry = self.call("search_transport", {**FLIGHT, "service_class": "any", "max_results": 5})
        self.handler = supplier
        success = self.call("search_transport", FLIGHT)
        refresh = self.call("search_transport", FLIGHT)
        self.assertEqual([r["attempt"] for r in (first, other, retry, success, refresh)], [1, 1, 2, 3, 1])
        self.assertEqual(retry["retry_of"], first["call_id"])
        self.assertEqual(len(self.tools.history), 5)

    def test_snapshot_restores_history_without_query_and_remains_independent(self):
        identifier = self.candidate()
        snapshot = self.tools.snapshot()
        restored = RealTools("restored", request=REQUEST, evidence_dir=self.folder.name,
            transport_http=self.tools.transport_http)
        restored.restore(snapshot)
        self.assertEqual(len(self.requests), 1)
        snapshot["history"].clear()
        self.assertTrue(restored.history)
        detail = restored.execute("get_transport_detail", {"candidate_id": identifier})
        self.assertEqual(detail["status"], "ok")
        with self.assertRaises(ValueError):
            restored.restore({})
