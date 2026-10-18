import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from travel_agent.agent import plan_trip
from travel_agent.providers import tool_call
from travel_agent.telemetry import Store
from travel_agent import hil
from test_transport import REQUEST
from test_detail_contract import rpc


class BoundModel:
    name, model = "offline", "bound-place"

    def __init__(self, honor_human=True):
        self.honor_human = honor_human

    def chat(self, messages, schemas, timeout):
        results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        count = len(results)
        if count == 0:
            name, args = "search_transport", {"mode": "flight", "origin": "杭州", "destination": "三亚", "departure_date": "2026-09-18"}
        elif count == 1:
            name, args = "get_transport_detail", {"candidate_id": results[0]["data"]["candidates"][0]["candidate_id"]}
        elif count == 2:
            name, args = "resolve_place", {"role": "arrival", "candidate_id": results[0]["data"]["candidates"][0]["candidate_id"]}
        elif count == 3:
            name, args = "search_stays", {"destination": "三亚", "check_in": "2026-09-18", "check_out": "2026-09-19"}
        elif count == 4:
            name, args = "resolve_place", {"role": "hotel", "candidate_id": results[3]["data"]["candidates"][0]["candidate_id"]}
        else:
            name, args = "finish", {"status": "needs_review", "reason": "missing_data", "segment_ids": [],
                                    "evidence_ids": [e for r in results for e in r["evidence_refs"]]}
        if "candidate_id" in args and self.honor_human:
            search = results[3] if count == 4 else results[0]
            choices = [json.loads(m["content"].split(": ", 1)[1])["human_response"].get("option_id")
                       for m in messages if m["role"] == "user" and m["content"].startswith("Application human context: ")]
            args["candidate_id"] = next((c for c in reversed(choices) if c and c.startswith(search["call_id"] + ":candidate:")), args["candidate_id"])
        return {"message": tool_call(name, args), "input_tokens": 0, "output_tokens": 0}


class PlaceModel:
    name, model = "offline", "place-contract"

    def chat(self, messages, schemas, timeout):
        results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        if len(results) < 2:
            name, args = "resolve_place", {"role": "meeting"}
        else:
            name, args = "finish", {"status": "needs_review", "reason": "missing_data", "segment_ids": [],
                                    "evidence_ids": [e for r in results for e in r["evidence_refs"]]}
        return {"message": tool_call(name, args), "input_tokens": 0, "output_tokens": 0}


class OnePlaceModel(PlaceModel):
    def __init__(self, args=None):
        self.args = args or {"role": "meeting"}

    def chat(self, messages, schemas, timeout):
        if not any(m["role"] == "tool" for m in messages):
            return {"message": tool_call("resolve_place", self.args), "input_tokens": 0, "output_tokens": 0}
        results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        return {"message": tool_call("finish", {"status": "needs_review", "reason": "missing_data", "segment_ids": [],
                "evidence_ids": [e for r in results for e in r["evidence_refs"]]}), "input_tokens": 0, "output_tokens": 0}


class PlaceContractTests(unittest.TestCase):
    def test_hil_place_choice_is_persistent_without_rewriting_supplier_confirmation(self):
        supplier = lambda _: httpx.Response(200, json={"status": "1", "pois": [
            {"name": "会场东门", "location": "109.5,18.2"}, {"name": "会场西门", "location": "109.6,18.3"}]})
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"AMAP_API_KEY": "test-secret"}):
            store = Store(Path(directory) / "trace.sqlite3")
            runtime = {"transport_http": httpx.MockTransport(supplier)}
            waiting = hil.start_trip({"id": "poi-hil", "request": {**REQUEST, "meeting_at": "2099-09-19T09:00+08:00"}},
                                     OnePlaceModel(), store, tool_mode="real", confirm_request=False, **runtime)
            self.assertEqual(waiting["execution_status"], "waiting_for_human")
            options = waiting["human_request"]["options"]
            self.assertEqual(options[1]["record"]["location"], "109.6,18.3")
            self.assertFalse(options[1]["record"]["identity_confirmed"])
            done = hil.respond_trip(waiting["run_id"], Store(store.path), lambda _: OnePlaceModel(),
                interaction_id=waiting["human_request"]["interaction_id"], state_version=waiting["state_version"],
                response_id="place-choice", reply={"option_id": options[1]["option_id"]}, **runtime)
            self.assertEqual(done["result"]["termination"], "completed")
            self.assertFalse(done["result"]["tool_results"][0]["data"]["identity_confirmed"])

    def test_hil_cannot_resolve_a_hotel_other_than_the_accepted_candidate(self):
        class IgnoreHotel(OnePlaceModel):
            def chat(self, messages, schemas, timeout):
                results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
                if not results:
                    call = tool_call("search_stays", {"destination": "三亚", "check_in": "2099-09-18", "check_out": "2099-09-19"})
                else:
                    call = tool_call("resolve_place", {"role": "hotel", "candidate_id": results[0]["data"]["candidates"][0]["candidate_id"]})
                return {"message": call, "input_tokens": 0, "output_tokens": 0}
        def supplier(request):
            self.assertEqual(request.method, "POST")
            return rpc(request, {"hotels": [{"hotelId": "11", "hotelName": "第一家"}, {"hotelId": "22", "hotelName": "第二家"}]})
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"AMAP_API_KEY": "test-secret", "TUNIU_API_KEY": "test-secret"}):
            store = Store(Path(directory) / "trace.sqlite3")
            runtime = {"transport_http": httpx.MockTransport(supplier)}
            waiting = hil.start_trip({"id": "ignore-hotel", "request": {**REQUEST, "meeting_at": "2099-09-19T09:00+08:00"}},
                                     IgnoreHotel(), store, tool_mode="real", confirm_request=False, **runtime)
            done = hil.respond_trip(waiting["run_id"], Store(store.path), lambda _: IgnoreHotel(),
                interaction_id=waiting["human_request"]["interaction_id"], state_version=waiting["state_version"],
                response_id="hotel-choice", reply={"option_id": waiting["human_request"]["options"][1]["option_id"]}, **runtime)
            self.assertEqual((done["result"]["termination"], done["result"]["http_requests"]), ("invalid_response", 1))

    def run_place(self, supplier, model=None, key="test-secret"):
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"AMAP_API_KEY": key}):
            store = Store(Path(directory) / "trace.sqlite3")
            run = plan_trip({"id": "place-errors", "request": REQUEST}, model or OnePlaceModel(), store,
                            tool_mode="real", transport_http=httpx.MockTransport(supplier))
            files = [p.read_text(encoding="utf-8") for p in Path(directory).rglob("place-*.json")]
            return run, files

    def test_unobserved_sources_and_changed_meeting_are_blocked_before_http(self):
        for args in ({"role": "hotel", "candidate_id": "foreign"}, {"role": "arrival", "candidate_id": "foreign"},
                     {"role": "meeting", "keyword": "另一会场"}, {"role": "meeting", "city": "北京"},
                     {"role": "meeting", "candidate_id": "foreign"}):
            with self.subTest(args=args):
                run, _ = self.run_place(lambda _: self.fail("Unexpected HTTP"), OnePlaceModel(args))
                self.assertEqual((run["termination"], run["http_requests"]), ("invalid_response", 0))

    def test_ambiguity_mismatch_and_invalid_coordinates_remain_uncertain(self):
        run, _ = self.run_place(lambda _: httpx.Response(200, json={"status": "1", "pois": [
            {"name": "另一会场", "cityname": "北京市", "location": "NaN,18.2"},
            {"name": "三亚湾红树林国际会议中心", "cityname": "三亚市", "location": "109.5,18.2"}]}))
        result = run["tool_results"][0]
        self.assertEqual(result["status"], "ok")
        first, second = result["data"]["candidates"]
        self.assertEqual(first["match_confidence"], "uncertain")
        self.assertIn("invalid_coordinates", first["uncertainty_reasons"])
        self.assertIn("city_not_verified", first["uncertainty_reasons"])
        self.assertIn("multiple_candidates", second["uncertainty_reasons"])
        self.assertFalse(second["identity_confirmed"])
        self.assertEqual(run["status"], "needs_review")

    def test_name_suffix_and_detail_airport_conflicts_remain_uncertain(self):
        run, _ = self.run_place(lambda _: httpx.Response(200, json={"status": "1", "pois": [
            {"name": "三亚湾红树林国际会议中心停车场", "cityname": "三亚市", "location": "109.5,18.2"}]}))
        candidate = run["tool_results"][0]["data"]["candidates"][0]
        self.assertEqual(candidate["match_confidence"], "uncertain")
        self.assertIn("name_not_exact", candidate["uncertainty_reasons"])

        class AirportModel:
            name, model = "offline", "airport-conflict"
            def chat(self, messages, schemas, timeout):
                results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
                if not results:
                    call = tool_call("search_transport", {"mode": "flight", "origin": "杭州", "destination": "三亚", "departure_date": "2026-09-18"})
                elif len(results) == 1:
                    call = tool_call("get_transport_detail", {"candidate_id": results[0]["data"]["candidates"][0]["candidate_id"]})
                elif len(results) == 2:
                    call = tool_call("resolve_place", {"role": "arrival", "candidate_id": results[0]["data"]["candidates"][0]["candidate_id"]})
                else:
                    call = tool_call("finish", {"status": "needs_review", "reason": "missing_data", "segment_ids": [], "evidence_ids": []})
                return {"message": call, "input_tokens": 0, "output_tokens": 0}
        def supplier(request):
            if request.method == "GET":
                return httpx.Response(200, json={"status": "1", "pois": [{"name": "凤凰", "cityname": "三亚", "location": "109.4,18.3"}]})
            params = json.loads(request.content)["params"]
            if params["name"] == "searchLowestPriceFlight":
                return rpc(request, {"data": [{"flightNumber": "CA1", "arrivalAirport": "凤凰",
                    "departureTime": "2026-09-18 10:00:00", "arrivalTime": "2026-09-18 12:00:00"}]})
            return rpc(request, {"flightInfo": {"flightNumber": "CA1", "arrivalAirport": "北京首都国际机场", "arrivalTerminal": "T2"}, "cabinInfo": []})
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"AMAP_API_KEY": "test-secret", "TUNIU_API_KEY": "test-secret"}):
            run = plan_trip({"id": "airport-conflict", "request": REQUEST}, AirportModel(), Store(Path(directory) / "trace.sqlite3"),
                            tool_mode="real", transport_http=httpx.MockTransport(supplier))
        place = run["tool_results"][2]
        self.assertEqual(place["data"]["keyword"], "凤凰机场T2")
        self.assertIn("arrival_airport_detail_mismatch", place["data"]["source_conflicts"])
        self.assertIn("arrival_airport_detail_mismatch", place["data"]["candidates"][0]["uncertainty_reasons"])

    def test_geocode_address_supplements_a_missing_supplier_address(self):
        def supplier(request):
            if request.url.path.endswith("/text"):
                return httpx.Response(200, json={"status": "1", "pois": [{"id": "p1", "name": "三亚湾红树林国际会议中心",
                    "address": "", "cityname": "三亚市", "location": "109.5,18.2"}]})
            if request.url.path.endswith("/detail"):
                return httpx.Response(200, json={"status": "1", "pois": [{"id": "p1", "address": ""}]})
            return httpx.Response(200, json={"status": "1", "geocodes": [{"formatted_address": "海南省三亚市凤凰路155号",
                "location": "109.5,18.2"}]})
        run, _ = self.run_place(supplier)
        candidate = run["tool_results"][0]["data"]["candidates"][0]
        self.assertEqual((candidate["address"], candidate["supplier_address"], candidate["address_source"]),
                         ("海南省三亚市凤凰路155号", "", "geocode"))
        self.assertNotIn("address_unavailable", candidate["uncertainty_reasons"])

    def test_supplier_failures_are_structured_and_secrets_are_not_exposed(self):
        for response in (httpx.Response(200, json={"status": "0", "info": "test-secret"}),
                         httpx.Response(200, json={"status": "1", "pois": {}}),
                         httpx.Response(200, json={"status": "1", "pois": [None]}),
                         httpx.Response(401, text="test-secret"), httpx.Response(200, text="not-json")):
            run, files = self.run_place(lambda _: response)
            self.assertEqual(run["termination"], "completed")
            result = run["tool_results"][0]
            self.assertEqual((result["status"], result["data"], result["http_requests"]), ("error", None, 1))
            self.assertFalse(result["error"]["retryable"])
            self.assertNotIn("test-secret", json.dumps(run) + "".join(files))
        run, _ = self.run_place(lambda _: self.fail("Missing key must not issue HTTP"), key="")
        self.assertEqual(run["tool_results"][0]["error"]["type"], "ConfigurationError")
        self.assertEqual(run["http_requests"], 0)

    def test_partial_detail_failure_keeps_primary_coordinates_and_counts_all_attempts(self):
        def supplier(request):
            if request.url.path.endswith("/text"):
                return httpx.Response(200, json={"status": "1", "pois": [{"id": "p1", "name": "三亚湾红树林国际会议中心",
                    "address": "凤凰路", "cityname": "三亚市", "location": "109.5101,18.2802"}]})
            if request.url.path.endswith("/detail"):
                raise httpx.ReadTimeout("test-secret", request=request)
            return httpx.Response(200, json={"status": "1", "geocodes": [{"location": "110.0,19.0"}]})
        run, files = self.run_place(supplier)
        result = run["tool_results"][0]
        first = result["data"]["candidates"][0]
        self.assertEqual((result["status"], run["http_requests"], len(files)), ("ok", 3, 3))
        self.assertEqual(first["location"], "109.5101,18.2802")
        self.assertEqual(first["detail_error"]["type"], "PlaceTimeout")
        self.assertIn("geocode_coordinates_differ", first["uncertainty_reasons"])
        self.assertEqual(run["retries"], 0)
        self.assertNotIn("test-secret", json.dumps(run) + "".join(files))

    def test_search_timeout_can_be_requeried_without_overwriting_failure(self):
        calls = []
        def supplier(request):
            calls.append(request)
            if len(calls) == 1:
                raise httpx.ReadTimeout("test-secret", request=request)
            return httpx.Response(200, json={"status": "1", "pois": []})
        run, files = self.run_place(supplier, PlaceModel())
        self.assertEqual((run["termination"], run["http_requests"], run["retries"], len(files)), ("completed", 2, 1, 2))
        first, second = run["tool_results"]
        self.assertEqual(second["retry_of"], first["call_id"])
        self.assertNotEqual(first["evidence_refs"], second["evidence_refs"])

    def test_hil_restores_independent_transport_and_hotel_place_sources(self):
        keywords = []
        def supplier(request):
            if request.method == "GET":
                keywords.append(request.url.params["keywords"])
                return httpx.Response(200, json={"status": "1", "pois": []})
            params = json.loads(request.content)["params"]
            payloads = {"searchLowestPriceFlight": {"data": [{"flightNumber": "CA1", "arrivalAirport": "错误"}, {"flightNumber": "CA2", "arrivalAirport": "凤凰"}]},
                        "multiCabinDetails": {"flightInfo": {"flightNumber": "CA2", "arrivalAirport": "凤凰", "arrivalTerminal": "T1"}, "cabinInfo": []},
                        "tuniuHotelSearch": {"hotels": [{"hotelId": "11", "hotelName": "第一家"}, {"hotelId": "22", "hotelName": "第二家"}]}}
            return rpc(request, payloads[params["name"]])
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"AMAP_API_KEY": "test-secret", "TUNIU_API_KEY": "test-secret"}):
            store = Store(Path(directory) / "trace.sqlite3")
            runtime = {"transport_http": httpx.MockTransport(supplier)}
            waiting = hil.start_trip({"id": "place-hil", "request": {**REQUEST, "meeting_at": "2099-09-19T09:00+08:00"}},
                                     BoundModel(), store, tool_mode="real", confirm_request=False, **runtime)
            for index in range(2):
                self.assertEqual(waiting["execution_status"], "waiting_for_human")
                store = Store(store.path)
                self.assertEqual(hil.resume_trip(waiting["run_id"], store, lambda _: BoundModel(), **runtime), waiting)
                kwargs = {"interaction_id": waiting["human_request"]["interaction_id"], "state_version": waiting["state_version"],
                          "response_id": f"choice-{index}", "reply": {"option_id": waiting["human_request"]["options"][1]["option_id"]}, **runtime}
                waiting = hil.respond_trip(waiting["run_id"], store, lambda _: BoundModel(), **kwargs)
            self.assertEqual(waiting["result"]["termination"], "completed")
            self.assertEqual(keywords, ["凤凰机场T1", "第二家"])
            self.assertEqual(hil.respond_trip(waiting["run_id"], store, lambda _: BoundModel(), **kwargs), waiting)
            self.assertEqual(len(keywords), 2)

    def test_arrival_terminal_and_hotel_address_are_bound_to_observed_objects(self):
        keywords = []
        def supplier(request):
            if request.method == "GET":
                keywords.append(request.url.params["keywords"])
                return httpx.Response(200, json={"status": "1", "pois": []})
            payloads = {"searchLowestPriceFlight": {"data": [{"flightNumber": "CA1", "arrivalAirport": "凤凰"}]},
                        "multiCabinDetails": {"flightInfo": {"flightNumber": "CA1", "arrivalAirport": "凤凰", "arrivalTerminal": "T1"}, "cabinInfo": []},
                        "tuniuHotelSearch": {"hotels": [{"hotelId": "22", "hotelName": "海韵酒店", "address": "三亚湾路168号"}]}}
            return rpc(request, payloads[json.loads(request.content)["params"]["name"]])
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"AMAP_API_KEY": "test-secret", "TUNIU_API_KEY": "test-secret"}):
            run = plan_trip({"id": "bound", "request": REQUEST}, BoundModel(), Store(Path(directory) / "trace.sqlite3"),
                            tool_mode="real", transport_http=httpx.MockTransport(supplier))
        self.assertEqual(run["termination"], "completed")
        self.assertEqual(keywords, ["凤凰机场T1", "海韵酒店"])
        arrival, hotel = run["tool_results"][2], run["tool_results"][4]
        self.assertEqual(arrival["data"]["source_candidate_id"], run["tool_results"][0]["data"]["candidates"][0]["candidate_id"])
        self.assertEqual(len(arrival["data"]["source_evidence_refs"]), 2)
        self.assertEqual(hotel["data"]["source_address"], "三亚湾路168号")

    def test_meeting_query_is_bound_to_request_and_repeated_evidence_survives(self):
        def supplier(request):
            if request.url.path.endswith("/detail"):
                return httpx.Response(200, json={"status": "1", "pois": [{"id": "p1", "name": "三亚湾红树林国际会议中心"}]})
            if request.url.path.endswith("/geo"):
                return httpx.Response(200, json={"status": "1", "geocodes": [{"location": "109.5,18.2"}]})
            self.assertEqual(request.url.params["keywords"], "三亚湾红树林国际会议中心")
            self.assertEqual(request.url.params["region"], "三亚")
            self.assertEqual(request.url.params["city_limit"], "true")
            return httpx.Response(200, json={"status": "1", "pois": [{"id": "p1",
                "name": "三亚湾红树林国际会议中心", "address": "凤凰路155号", "cityname": "三亚市",
                "location": "109.5101,18.2802"}]})
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"AMAP_API_KEY": "test-secret"}):
            store = Store(Path(directory) / "trace.sqlite3")
            run = plan_trip({"id": "places", "request": REQUEST}, PlaceModel(), store,
                            tool_mode="real", transport_http=httpx.MockTransport(supplier))
            self.assertEqual(run["termination"], "completed")
            first, second = run["tool_results"]
            self.assertEqual(first["data"]["candidates"][0]["location"], "109.5101,18.2802")
            self.assertEqual(first["data"]["candidates"][0]["address"], "凤凰路155号")
            self.assertFalse(first["data"]["identity_confirmed"])
            self.assertEqual(run["http_requests"], 6)
            self.assertEqual(len(first["evidence_refs"]), 3)
            self.assertEqual(first["data"]["candidates"][0]["geocode"]["location"], "109.5,18.2")
            self.assertNotEqual(first["evidence_refs"], second["evidence_refs"])
            self.assertEqual(store.get_run(run["trace_id"])["tool_results"], run["tool_results"])
