import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from travel_agent import hil
from travel_agent.agent import plan_trip
from travel_agent.providers import tool_call
from travel_agent.telemetry import Store
from test_detail_contract import rpc
from test_transport import REQUEST


MEETING_REQUEST = {**REQUEST, "meeting_at": "2099-09-19T09:00+08:00"}


class RouteModel:
    name, model = "offline", "route-contract"

    def chat(self, messages, schemas, timeout):
        results = [json.loads(m["content"]) for m in messages if m.get("role") == "tool"]
        if not results:
            call = tool_call("search_transport", {"mode": "flight", "origin": "杭州",
                "destination": "三亚", "departure_date": "2099-09-18"})
        elif len(results) == 1:
            call = tool_call("get_transport_detail", {"candidate_id": results[0]["data"]["candidates"][0]["candidate_id"]})
        elif len(results) == 2:
            call = tool_call("resolve_place", {"role": "arrival", "candidate_id": results[0]["data"]["candidates"][0]["candidate_id"]})
        elif len(results) == 3:
            call = tool_call("search_stays", {"destination": "三亚", "check_in": "2099-09-18", "check_out": "2099-09-19"})
        elif len(results) == 4:
            call = tool_call("resolve_place", {"role": "hotel", "candidate_id": results[3]["data"]["candidates"][0]["candidate_id"]})
        elif len(results) == 5:
            call = tool_call("resolve_place", {"role": "meeting"})
        elif len(results) == 6:
            call = tool_call("calculate_route", {
                "origin_candidate_id": results[2]["data"]["candidates"][0]["candidate_id"],
                "destination_candidate_id": results[4]["data"]["candidates"][0]["candidate_id"],
            })
        elif len(results) == 7:
            call = tool_call("calculate_route", {
                "origin_candidate_id": results[4]["data"]["candidates"][0]["candidate_id"],
                "destination_candidate_id": results[5]["data"]["candidates"][0]["candidate_id"],
            })
        else:
            call = tool_call("finish", {"status": "needs_review", "reason": "missing_data",
                "segment_ids": [], "evidence_ids": [ref for result in results for ref in result["evidence_refs"]]})
        return {"message": call, "input_tokens": 0, "output_tokens": 0}


def _route_payload(request):
    params = request.url.params
    if params.get("origin", "") == "109.1000,18.3000":
        distance, duration, tolls = "21000", "1260", "12"
    else:
        distance, duration, tolls = "5000", "942", None
    return httpx.Response(200, json={"status": "1", "route": {
        "origin": params.get("origin"), "destination": params.get("destination"),
        "taxi_cost": "88", "paths": [{"id": "R1", "distance": distance, "duration": duration,
            "cost": {"tolls": tolls} if tolls is not None else {}, "steps": []}],
    }})


def route_supplier(request):
    if request.method == "GET":
        path = request.url.path
        if path.endswith("/text"):
            keyword = request.url.params["keywords"]
            places = {
                "凤凰机场T1": {"id": "airport", "name": "凤凰机场T1", "address": "机场路", "cityname": "三亚市", "location": "109.1000,18.3000"},
                "海韵酒店": {"id": "hotel", "name": "海韵酒店", "address": "三亚湾路168号", "cityname": "三亚市", "location": "109.1100,18.3100"},
                "三亚湾红树林国际会议中心": {"id": "meeting", "name": "三亚湾红树林国际会议中心", "address": "凤凰路155号", "cityname": "三亚市", "location": "109.1200,18.3200"},
            }
            return httpx.Response(200, json={"status": "1", "pois": [places[keyword]]})
        if path.endswith("/detail"):
            return httpx.Response(200, json={"status": "1", "pois": [{"id": request.url.params["id"], "address": "三亚湾路168号"}]})
        if path.endswith("/geo"):
            return httpx.Response(200, json={"status": "1", "geocodes": [{"location": "109.1100,18.3100"}]})
        if path.endswith("/driving"):
            return _route_payload(request)
    params = json.loads(request.content)["params"]
    if params["name"] == "searchLowestPriceFlight":
        return rpc(request, {"data": [{"flightNumber": "HU1", "arrivalAirport": "凤凰", "departureTime": "2099-09-18 16:05:00", "arrivalTime": "2099-09-18 18:55:00"}]})
    if params["name"] == "multiCabinDetails":
        return rpc(request, {"flightInfo": {"flightNumber": "HU1", "arrivalAirport": "凤凰", "arrivalTerminal": "T1"}, "cabinInfo": []})
    return rpc(request, {"hotels": [{"hotelId": "h1", "hotelName": "海韵酒店", "address": "三亚湾路168号"}]})


class RouteContractTests(unittest.TestCase):
    def test_plan_trip_keeps_two_route_evidence_and_cost_semantics(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"AMAP_API_KEY": "amap-secret", "TUNIU_API_KEY": "tuniu-secret"}):
            store = Store(Path(directory) / "trace.sqlite3")
            run = plan_trip({"id": "route-contract", "request": MEETING_REQUEST}, RouteModel(), store,
                            tool_mode="real", transport_http=httpx.MockTransport(route_supplier))
            self.assertEqual((run["termination"], run["status"]), ("completed", "needs_review"))
            routes = [result for result in run["tool_results"] if result["tool"] == "calculate_route"]
            self.assertEqual(len(routes), 2)
            first, second = routes
            self.assertEqual(first["data"]["origin"]["role"], "arrival")
            self.assertEqual((first["data"]["destination"]["role"], second["data"]["origin"]["role"]), ("hotel", "hotel"))
            self.assertEqual(second["data"]["destination"]["role"], "meeting")
            self.assertEqual(first["data"]["destination_candidate_id"], second["data"]["origin_candidate_id"])
            self.assertNotEqual(first["data"]["paths"][0]["route_id"], second["data"]["paths"][0]["route_id"])
            self.assertEqual((first["data"]["paths"][0]["supplier_route_id"], second["data"]["paths"][0]["supplier_route_id"]), ("R1", "R1"))
            self.assertNotEqual(first["evidence_refs"], second["evidence_refs"])
            self.assertEqual(first["data"]["paths"][0]["duration_basis"], "query_time_estimate")
            self.assertEqual(first["data"]["paths"][0]["toll_cny"], "12")
            self.assertIsNone(second["data"]["paths"][0]["toll_cny"])
            self.assertEqual(first["data"]["paths"][0]["taxi_fare_cny"], "88")
            self.assertEqual(run["http_requests"], 14)
            self.assertEqual(store.get_run(run["trace_id"])["tool_results"], run["tool_results"])
            evidence = list((Path(directory) / "evidence" / run["trace_id"]).glob("route-*.json"))
            self.assertEqual(len(evidence), 2)
            self.assertNotIn("amap-secret", json.dumps(run) + "".join(p.read_text(encoding="utf-8") for p in evidence))

    def test_route_requires_observed_place_candidates_and_rejects_wrong_role_pair(self):
        class BadRoute(RouteModel):
            def chat(self, messages, schemas, timeout):
                results = [json.loads(m["content"]) for m in messages if m.get("role") == "tool"]
                if len(results) == 6:
                    return {"message": tool_call("calculate_route", {
                        "origin_candidate_id": results[5]["data"]["candidates"][0]["candidate_id"],
                        "destination_candidate_id": results[4]["data"]["candidates"][0]["candidate_id"],
                    }), "input_tokens": 0, "output_tokens": 0}
                return super().chat(messages, schemas, timeout)
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"AMAP_API_KEY": "amap-secret", "TUNIU_API_KEY": "tuniu-secret"}):
            run = plan_trip({"id": "route-invalid", "request": MEETING_REQUEST}, BadRoute(), Store(Path(directory) / "trace.sqlite3"),
                            tool_mode="real", transport_http=httpx.MockTransport(route_supplier))
        self.assertEqual((run["termination"], run["http_requests"], run["guardrail_blocks"]), ("invalid_response", 12, 1))

    def test_hil_selected_hotel_is_used_by_both_routes_after_resume(self):
        class HilRoute(RouteModel):
            def chat(self, messages, schemas, timeout):
                results = [json.loads(m["content"]) for m in messages if m.get("role") == "tool"]
                if len(results) == 3:
                    return {"message": tool_call("search_stays", {"destination": "三亚", "check_in": "2099-09-18", "check_out": "2099-09-19"}), "input_tokens": 0, "output_tokens": 0}
                if len(results) == 4:
                    choices = [json.loads(m["content"].split(": ", 1)[1])["human_response"].get("option_id")
                               for m in messages if m.get("role") == "user" and m.get("content", "").startswith("Application human context: ")]
                    return {"message": tool_call("resolve_place", {"role": "hotel", "candidate_id": choices[-1]}), "input_tokens": 0, "output_tokens": 0}
                return super().chat(messages, schemas, timeout)
        def supplier(request):
            if request.method == "GET" and request.url.path.endswith("/text"):
                keyword = request.url.params["keywords"]
                if keyword == "海韵酒店B":
                    row = {"id": "hotel-b", "name": keyword, "address": "三亚湾路188号", "cityname": "三亚市", "location": "109.1150,18.3150"}
                elif keyword == "海韵酒店A":
                    row = {"id": "hotel-a", "name": keyword, "address": "三亚湾路168号", "cityname": "三亚市", "location": "109.1100,18.3100"}
                else:
                    return route_supplier(request)
                return httpx.Response(200, json={"status": "1", "pois": [row]})
            if request.method == "POST" and json.loads(request.content)["params"]["name"] == "tuniuHotelSearch":
                return rpc(request, {"hotels": [{"hotelId": "a", "hotelName": "海韵酒店A"}, {"hotelId": "b", "hotelName": "海韵酒店B"}]})
            return route_supplier(request)
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"AMAP_API_KEY": "amap-secret", "TUNIU_API_KEY": "tuniu-secret"}):
            store = Store(Path(directory) / "trace.sqlite3")
            runtime = {"transport_http": httpx.MockTransport(supplier)}
            waiting = hil.start_trip({"id": "route-hil", "request": MEETING_REQUEST}, HilRoute(), store,
                                     tool_mode="real", confirm_request=False, **runtime)
            self.assertEqual(waiting["execution_status"], "waiting_for_human")
            options = waiting["human_request"]["options"]
            selected = options[1]["option_id"]
            done = hil.respond_trip(waiting["run_id"], Store(store.path), lambda _: HilRoute(),
                interaction_id=waiting["human_request"]["interaction_id"], state_version=waiting["state_version"],
                response_id="hotel-b", reply={"option_id": selected}, **runtime)
            self.assertEqual(done["result"]["termination"], "completed")
            routes = [result for result in done["result"]["tool_results"] if result["tool"] == "calculate_route"]
            self.assertEqual(len(routes), 2)
            self.assertEqual(routes[0]["data"]["destination"]["name"], "海韵酒店B")
            self.assertEqual(routes[1]["data"]["origin"]["name"], "海韵酒店B")


if __name__ == "__main__":
    unittest.main()
