import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from travel_agent import hil
from travel_agent.agent import Limits, plan_trip
from travel_agent.providers import tool_call
from travel_agent.telemetry import Store
import test_transport as transport_tests


REQUEST = {**transport_tests.REQUEST, "meeting_at": "2099-09-19T09:00+08:00"}


class DetailModel:
    name, model = "offline", "detail-contract"

    def __init__(self, mode="flight", override=None, retries=0, ignore_human=False):
        self.mode, self.override, self.retries, self.ignore_human = mode, override, retries, ignore_human

    def chat(self, messages, schemas, timeout):
        results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        if not results:
            call = tool_call("search_transport", {"mode": self.mode, "origin": "杭州",
                "destination": "三亚", "departure_date": "2099-09-18"})
        elif len(results) < 2 + self.retries:
            identifier = results[0]["data"]["candidates"][0]["candidate_id"]
            if not self.ignore_human:
                for m in messages:
                    if m["role"] == "user" and m["content"].startswith("Application human context: "):
                        reply = json.loads(m["content"].split(": ", 1)[1])["human_response"]
                        identifier = reply.get("option_id", identifier)
            call = tool_call("get_transport_detail", self.override if self.override is not None else {"candidate_id": identifier})
        else:
            call = tool_call("finish", {"status": "needs_review", "reason": "missing_data",
                "segment_ids": [], "evidence_ids": [ref for r in results for ref in r["evidence_refs"]]})
        return {"message": call, "input_tokens": 0, "output_tokens": 0}


def rpc(request, payload):
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": json.loads(request.content)["id"],
                                    "result": {"structuredContent": payload}})


def flight_rows():
    return [{"flightNumber": n, "departureTime": "2099-09-18 16:05:00",
             "arrivalTime": "2099-09-18 18:55:00"} for n in ("HU7386", "CA9999")]


class DetailContractTests(unittest.TestCase):
    def run_detail(self, detail, model=None, rows=None, limits=None):
        def supplier(request):
            if json.loads(request.content)["params"]["name"].startswith("searchLowest"):
                return rpc(request, {"data": rows if rows is not None else flight_rows()[:1]})
            return detail(request)
        return transport_tests.TransportTests.run_supplier(self, supplier, model or DetailModel(), REQUEST, limits)

    def test_flight_detail_preserves_terminal_precision_and_both_evidence_links(self):
        def detail(request):
            self.assertEqual(json.loads(request.content)["params"], {"name": "multiCabinDetails", "arguments": {
                "departureCityName": "杭州", "arrivalCityName": "三亚", "departureDate": "2099-09-18", "flightNo": "HU7386"}})
            return rpc(request, {"flightInfo": [{"flightNumber": "HU7386", "arrivalAirport": "凤凰机场",
                "arrivalTerminal": "T3", "departureTerminal": "T4"}], "cabinInfo": [
                    {"basePrice": "123.45010001", "totalTax": None, "cabinClass": "经济舱", "baggageInfo": "20kg"}]})
        run, trace, files = self.run_detail(detail)
        search, detail = run["tool_results"]
        self.assertEqual((run["termination"], run["status"]), ("completed", "needs_review"))
        self.assertEqual(detail["data"]["candidate_id"], search["data"]["candidates"][0]["candidate_id"])
        self.assertEqual(detail["data"]["candidate_evidence_ref"], search["evidence_refs"][0])
        self.assertNotEqual(search["evidence_refs"], detail["evidence_refs"])
        self.assertEqual(detail["data"]["flight"]["arrivalTerminal"], "T3")
        self.assertEqual(detail["data"]["cabin_options"][0]["basePrice"], "123.45010001")
        self.assertIsNone(detail["data"]["cabin_options"][0]["totalTax"])
        self.assertEqual(trace["tool_results"], run["tool_results"])
        self.assertEqual(len(files), 2)

    def test_wrong_service_date_is_not_accepted_as_selected_detail(self):
        run, _, _ = self.run_detail(lambda req: rpc(req, {"flightInfo": {
            "flightNo": "HU7386", "departureDate": "2099-09-17"}, "cabinInfo": []}))
        self.assertEqual(run["termination"], "completed")
        self.assertEqual(run["tool_results"][1]["status"], "error")
        self.assertIsNone(run["tool_results"][1]["data"])

    def test_unobserved_and_malformed_candidate_ids_never_make_detail_requests(self):
        for args in ({"candidate_id": "foreign:tool:1:candidate:1"}, {"candidate_id": 1},
                     {"candidate_id": "flight:HU7386"}, {"candidate_id": "x", "flightNo": "HU7386"}):
            with self.subTest(args=args):
                run, _, _ = self.run_detail(lambda req: self.fail("Unexpected detail HTTP"), DetailModel(override=args))
                self.assertEqual((run["termination"], run["guardrail_blocks"], run["http_requests"]),
                                 ("invalid_response", 1, 1))

    def test_missing_candidate_fields_make_no_http_and_no_fabricated_detail(self):
        run, _, _ = self.run_detail(lambda req: self.fail("Unexpected HTTP"), rows=[{}])
        self.assertEqual(run["termination"], "completed")
        result = run["tool_results"][1]
        self.assertEqual((result["error"]["type"], result["http_requests"], result["data"]),
                         ("MissingCandidateData", 0, None))

    def test_supplier_wrong_identity_and_bad_schema_preserve_failure_evidence(self):
        for payload in ({"flightInfo": {"flightNo": "OTHER"}, "cabinInfo": []},
                        {"flightInfo": {}, "cabinInfo": {}}, {"data": []},
                        {"flightInfo": [{}, {}], "cabinInfo": []}):
            with self.subTest(payload=payload):
                run, trace, files = self.run_detail(lambda req: rpc(req, payload))
                result = run["tool_results"][1]
                self.assertEqual((result["status"], result["data"], result["error"]["retryable"]), ("error", None, False))
                self.assertEqual(len(files), 2)
                self.assertEqual(trace["tool_results"], run["tool_results"])

    def test_train_detail_uses_observed_stations_and_preserves_decimal_quotes(self):
        def detail(request):
            self.assertEqual(json.loads(request.content)["params"], {"name": "queryTrainDetail", "arguments": {
                "departureStationName": "杭州东", "arrivalStationName": "三亚", "departureDate": "2099-09-18", "trainNum": "G123"}})
            body = '{"jsonrpc":"2.0","id":' + json.dumps(json.loads(request.content)["id"]) + ',"result":{"structuredContent":{"data":{"trainInfo":{"trainNum":"G123"},"seatInfo":[{"seatName":"二等座","price":123.45010001,"leftNumber":null}]}}}}'
            return httpx.Response(200, content=body)
        run, _, _ = self.run_detail(detail, DetailModel(mode="train"), rows=[{"trainNum": "G123",
            "departStationName": "杭州东", "destStationName": "三亚"}])
        result = run["tool_results"][1]
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["data"]["seat_options"][0]["price"], "123.45010001")
        self.assertIsNone(result["data"]["seat_options"][0]["leftNumber"])

    def test_train_detail_rejects_different_date_or_stations(self):
        for field, value in (("departsDate", "2099-09-17"), ("departStationName", "杭州西"),
                             ("destStationName", "海口")):
            with self.subTest(field=field):
                run, _, _ = self.run_detail(lambda req: rpc(req, {"data": {"trainInfo": {
                    "trainNum": "G123", field: value}, "seatInfo": []}}),
                    DetailModel(mode="train"), rows=[{"trainNum": "G123", "departStationName": "杭州东", "destStationName": "三亚"}])
                self.assertEqual(run["tool_results"][1]["status"], "error")
                self.assertIsNone(run["tool_results"][1]["data"])

    def test_detail_retry_is_model_driven_and_counts_attempts_and_budget(self):
        attempts = []
        def detail(request):
            attempts.append(request)
            if len(attempts) < 3:
                raise httpx.ReadTimeout("test-secret")
            return rpc(request, {"flightInfo": {"flightNo": "HU7386"}, "cabinInfo": []})
        run, trace, _ = self.run_detail(detail, DetailModel(retries=2))
        details = run["tool_results"][1:]
        self.assertEqual((run["termination"], run["http_requests"], run["retries"], run["model_calls"]), ("completed", 4, 2, 5))
        self.assertEqual([r["attempt"] for r in details], [1, 2, 3])
        self.assertEqual(details[-1]["retry_of"], details[-2]["call_id"])
        self.assertNotIn("test-secret", json.dumps(trace))
        attempts.clear()
        run, _, _ = self.run_detail(detail, DetailModel(retries=2), limits=Limits(max_tool_calls=2))
        self.assertEqual((run["termination"], run["budget_reason"], run["http_requests"]), ("budget_exceeded", "tool_calls", 2))

    def test_hil_restore_uses_selected_candidate_and_blocks_other_candidate(self):
        for ignore in (False, True):
            with self.subTest(ignore_human=ignore), tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"TUNIU_API_KEY": "test-secret"}):
                seen = []
                def supplier(request):
                    params = json.loads(request.content)["params"]
                    seen.append(params)
                    if params["name"] == "searchLowestPriceFlight":
                        return rpc(request, {"data": flight_rows()})
                    self.assertEqual(params["arguments"]["flightNo"], "CA9999")
                    return rpc(request, {"flightInfo": {"flightNo": "CA9999", "arrivalAirport": "凤凰机场", "arrivalTerminal": "T3"}, "cabinInfo": []})
                store = Store(Path(directory) / "trace.db")
                runtime = {"transport_http": httpx.MockTransport(supplier)}
                waiting = hil.start_trip({"id": "detail-hil", "request": REQUEST}, DetailModel(ignore_human=ignore),
                                         store, tool_mode="real", confirm_request=False, **runtime)
                self.assertEqual(waiting["execution_status"], "waiting_for_human")
                selected = waiting["human_request"]["options"][1]["option_id"]
                store = Store(store.path)
                factory = lambda _: DetailModel(ignore_human=ignore)
                self.assertEqual(hil.resume_trip(waiting["run_id"], store, factory, **runtime), waiting)
                kwargs = {"interaction_id": waiting["human_request"]["interaction_id"], "state_version": waiting["state_version"],
                          "response_id": "select-second", "reply": {"option_id": selected}, **runtime}
                done = hil.respond_trip(waiting["run_id"], store, factory, **kwargs)
                run = done["result"]
                self.assertEqual(run["termination"], "invalid_response" if ignore else "completed")
                self.assertEqual(len(seen), 1 if ignore else 2)
                if not ignore:
                    detail = run["tool_results"][1]["data"]
                    self.assertEqual(detail["candidate_id"], selected)
                    self.assertEqual(detail["flight"]["arrivalTerminal"], "T3")
                    self.assertEqual(run["human_requests"], 1)
                self.assertEqual(hil.respond_trip(waiting["run_id"], store, factory, **kwargs), done)
                self.assertEqual(len(seen), 1 if ignore else 2)
