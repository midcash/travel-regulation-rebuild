import json
import unittest
from pathlib import Path
import tempfile
from unittest.mock import patch

import httpx
from travel_agent import hil
from travel_agent.telemetry import Store

from travel_agent.providers import tool_call
import test_transport as transport_tests
from test_detail_contract import rpc


class StayModel:
    name, model = "offline", "stay-contract"

    def __init__(self, search=None, detail=None, retries=0, choose_human=False):
        self.search, self.detail, self.retries, self.choose_human = search, detail, retries, choose_human

    def chat(self, messages, schemas, timeout):
        results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        if not results:
            name, args = "search_stays", {"destination": "三亚", "check_in": "2026-09-18", "check_out": "2026-09-19"}
            if self.search is not None:
                args = self.search
        elif len(results) <= 1 + self.retries:
            name, args = "get_stay_detail", {"candidate_id": results[0]["data"]["candidates"][0]["candidate_id"]}
            if self.choose_human:
                for m in messages:
                    if m["role"] == "user" and m["content"].startswith("Application human context: "):
                        args["candidate_id"] = json.loads(m["content"].split(": ", 1)[1])["human_response"]["option_id"]
            if self.detail is not None:
                args = self.detail
        else:
            name, args = "finish", {"status": "needs_review", "reason": "missing_data", "segment_ids": [],
                                    "evidence_ids": [ref for r in results for ref in r["evidence_refs"]]}
        return {"message": tool_call(name, args), "input_tokens": 0, "output_tokens": 0}


class StayContractTests(unittest.TestCase):
    def run_stay(self, payload, model=None, rows=None):
        def supplier(request):
            if json.loads(request.content)["params"]["name"] == "tuniuHotelSearch":
                return rpc(request, {"hotels": rows if rows is not None else [{"hotelId": "34548"}]})
            return payload(request)
        return transport_tests.TransportTests.run_supplier(self, supplier, model or StayModel())

    def test_invalid_hotel_dates_or_candidate_never_trigger_extra_http(self):
        for args in ({"candidate_id": "foreign"}, {"candidate_id": True},
                     {"candidate_id": "34548", "check_in": "2026-09-17"}):
            with self.subTest(args=args):
                run, _, _ = self.run_stay(lambda _: self.fail("Unexpected detail HTTP"), StayModel(detail=args))
                self.assertEqual((run["termination"], run["http_requests"], run["guardrail_blocks"]), ("invalid_response", 1, 1))
        for start, end in (("2026-09-19", "2026-09-19"), ("2026-09-20", "2026-09-19"), ("bad", "2026-09-19")):
            run, _, _ = self.run_stay(lambda _: self.fail("Unexpected HTTP"), StayModel(search={
                "destination": "三亚", "check_in": start, "check_out": end}))
            self.assertEqual((run["termination"], run["http_requests"]), ("invalid_response", 0))

    def test_missing_hotel_id_is_not_fabricated(self):
        run, _, _ = self.run_stay(lambda _: self.fail("Unexpected detail HTTP"), rows=[{"hotelName": "无ID酒店"}])
        self.assertEqual(run["tool_results"][1]["error"]["type"], "MissingCandidateData")
        self.assertEqual(run["http_requests"], 1)

    def test_different_stay_dates_remain_visible_without_satisfying_request(self):
        run, _, _ = self.run_stay(lambda req: rpc(req, {"hotelId": 34548, "roomTypes": []}), StayModel(search={
            "destination": "三亚", "check_in": "2026-09-17", "check_out": "2026-09-19"}))
        data = run["tool_results"][0]["data"]
        self.assertFalse(data["meets_request"])
        self.assertIn("wrong_stay_dates", data["constraint_reasons"])
        self.assertEqual(data["check_in"], "2026-09-17")

    def test_wrong_hotel_dates_and_invalid_room_shapes_keep_error_evidence(self):
        for payload in ({"hotelId": 99, "roomTypes": []}, {"hotelId": 34548, "roomTypes": {}},
                        {"roomTypes": [{"ratePlans": {}}]}, {"roomTypes": [None]},
                        {"roomTypes": [], "checkIn": "2026-09-17"}, {"roomTypes": [], "checkOut": "2026-09-20"}):
            with self.subTest(payload=payload):
                run, trace, files = self.run_stay(lambda req: rpc(req, payload))
                self.assertEqual(run["termination"], "completed")
                result = run["tool_results"][1]
                self.assertEqual((result["status"], result["data"], result["error"]["retryable"]), ("error", None, False))
                self.assertEqual(len(files), 2)
                self.assertEqual(trace["tool_results"], run["tool_results"])

    def test_detail_timeout_retries_are_model_driven_and_counted(self):
        calls = []
        def supplier(request):
            calls.append(request)
            if len(calls) < 3:
                raise httpx.ReadTimeout("test-secret", request=request)
            return rpc(request, {"hotelId": 34548, "roomTypes": []})
        run, _, files = self.run_stay(supplier, StayModel(retries=2))
        self.assertEqual((run["termination"], run["http_requests"], run["retries"]), ("completed", 4, 2))
        self.assertEqual([r["attempt"] for r in run["tool_results"]], [1, 1, 2, 3])
        self.assertNotIn("test-secret", json.dumps(run) + "".join(files))

    def test_hil_rejects_model_ignoring_selected_hotel(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"TUNIU_API_KEY": "test-secret"}):
            def supplier(request):
                self.assertEqual(json.loads(request.content)["params"]["name"], "tuniuHotelSearch")
                return rpc(request, {"hotels": [{"hotelId": "111"}, {"hotelId": "222"}]})
            store = Store(Path(directory) / "trace.sqlite3")
            runtime = {"transport_http": httpx.MockTransport(supplier)}
            waiting = hil.start_trip({"id": "stay-ignore", "request": {**transport_tests.REQUEST, "meeting_at": "2099-09-19T09:00+08:00"}},
                                     StayModel(), store, tool_mode="real", confirm_request=False, **runtime)
            self.assertEqual(waiting["execution_status"], "waiting_for_human")
            done = hil.respond_trip(waiting["run_id"], Store(store.path), lambda _: StayModel(),
                interaction_id=waiting["human_request"]["interaction_id"], state_version=waiting["state_version"],
                response_id="select-hotel", reply={"option_id": waiting["human_request"]["options"][1]["option_id"]}, **runtime)
            self.assertEqual((done["result"]["termination"], done["result"]["http_requests"]), ("invalid_response", 1))

    def test_hil_hotel_and_transport_choices_are_independent_after_restore(self):
        class CombinedModel:
            name, model = "offline", "combined-stay"

            def chat(self, messages, schemas, timeout):
                results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
                choices = [json.loads(m["content"].split(": ", 1)[1])["human_response"]["option_id"]
                           for m in messages if m["role"] == "user" and m["content"].startswith("Application human context: ")]
                count = len(results)
                if count == 0:
                    name, args = "search_transport", {"mode": "flight", "origin": "杭州", "destination": "三亚", "departure_date": "2099-09-18"}
                elif count == 1:
                    name, args = "get_transport_detail", {"candidate_id": choices[0]}
                elif count == 2:
                    name, args = "search_stays", {"destination": "三亚", "check_in": "2099-09-18", "check_out": "2099-09-19"}
                elif count == 3:
                    name, args = "get_stay_detail", {"candidate_id": choices[-1]}
                else:
                    name, args = "finish", {"status": "needs_review", "reason": "missing_data", "segment_ids": [],
                                             "evidence_ids": [ref for r in results for ref in r["evidence_refs"]]}
                return {"message": tool_call(name, args), "input_tokens": 0, "output_tokens": 0}
        calls = []
        def supplier(request):
            params = json.loads(request.content)["params"]
            calls.append(params)
            payloads = {"searchLowestPriceFlight": {"data": [{"flightNumber": n} for n in ("CA1", "CA2")]},
                        "multiCabinDetails": {"flightInfo": {"flightNumber": "CA2"}, "cabinInfo": []},
                        "tuniuHotelSearch": {"hotels": [{"hotelId": n} for n in ("111", "222")]},
                        "tuniuHotelDetail": {"hotelId": 222, "roomTypes": []}}
            return rpc(request, payloads[params["name"]])
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"TUNIU_API_KEY": "test-secret"}):
            store = Store(Path(directory) / "trace.sqlite3")
            runtime = {"transport_http": httpx.MockTransport(supplier)}
            waiting = hil.start_trip({"id": "stay-hil", "request": {**transport_tests.REQUEST, "meeting_at": "2099-09-19T09:00+08:00"}},
                                     CombinedModel(), store, tool_mode="real", confirm_request=False, **runtime)
            for index in range(2):
                self.assertEqual(waiting["execution_status"], "waiting_for_human")
                selected = waiting["human_request"]["options"][1]["option_id"]
                store = Store(store.path)
                self.assertEqual(hil.resume_trip(waiting["run_id"], store, lambda _: CombinedModel(), **runtime), waiting)
                kwargs = {"interaction_id": waiting["human_request"]["interaction_id"], "state_version": waiting["state_version"],
                          "response_id": f"choice-{index}", "reply": {"option_id": selected}, **runtime}
                waiting = hil.respond_trip(waiting["run_id"], store, lambda _: CombinedModel(), **kwargs)
            self.assertEqual(waiting["result"]["termination"], "completed")
            self.assertEqual(calls[-1]["arguments"]["hotelId"], 222)
            self.assertEqual(calls[1]["arguments"]["flightNo"], "CA2")
            self.assertEqual(waiting["result"]["human_requests"], 2)
            self.assertEqual(hil.respond_trip(waiting["run_id"], store, lambda _: CombinedModel(), **kwargs), waiting)
            self.assertEqual(len(calls), 4)

    def test_observed_hotel_dates_id_and_raw_rates_survive_main_chain(self):
        def supplier(request):
            params = json.loads(request.content)["params"]
            if params["name"] == "tuniuHotelSearch":
                self.assertEqual(params["arguments"], {"cityName": "三亚", "checkIn": "2026-09-18",
                                                      "checkOut": "2026-09-19", "adultNum": 1})
                return rpc(request, {"hotels": [{"hotelId": "34548", "hotelName": "海韵", "address": "三亚湾路",
                                                 "lowestPrice": "321.45010001"}]})
            self.assertEqual(params, {"name": "tuniuHotelDetail", "arguments": {"hotelId": 34548,
                "checkIn": "2026-09-18", "checkOut": "2026-09-19", "roomNum": 1, "adultNum": 1}})
            return rpc(request, {"hotelId": 34548, "hotelName": "海韵", "address": "三亚湾路",
                "policies": {"checkInTime": "15:00"}, "roomTypes": [{"roomTypeId": "r1",
                "ratePlans": [{"rmbPrices": "321.45010001", "cancelDesc": "不可退"}]}]})
        run, trace, files = transport_tests.TransportTests.run_supplier(self, supplier, StayModel())
        self.assertEqual(run["termination"], "completed")
        search, detail = run["tool_results"]
        self.assertEqual(search["data"]["candidates"][0]["lowest_price_raw"], "321.45010001")
        self.assertEqual(detail["data"]["check_in"], "2026-09-18")
        self.assertEqual(detail["data"]["check_out"], "2026-09-19")
        self.assertEqual(detail["data"]["price_basis"], "unknown")
        self.assertEqual(detail["data"]["hotel"]["address"], "三亚湾路")
        self.assertEqual(detail["data"]["room_types"][0]["ratePlans"][0]["rmbPrices"], "321.45010001")
        self.assertEqual(detail["data"]["candidate_evidence_ref"], search["evidence_refs"][0])
        self.assertEqual((run["http_requests"], len(files)), (2, 2))
        self.assertEqual(trace["tool_results"], run["tool_results"])
