import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from travel_agent import hil
from travel_agent.agent import plan_trip
from travel_agent.providers import tool_call
from travel_agent.telemetry import Store
from test_route_contract import MEETING_REQUEST, route_supplier


class CompleteItineraryModel:
    name, model = "offline", "real-itinerary-contract"

    def __init__(self, departure="2099-09-19T08:00:00+08:00", buffer=30, finish_mutation=None):
        self.departure = departure
        self.buffer = buffer
        self.finish_mutation = finish_mutation

    def chat(self, messages, schemas, timeout):
        results = [json.loads(message["content"]) for message in messages
                   if message.get("role") == "tool"]
        if len(results) == 0:
            call = tool_call("search_transport", {"mode": "flight", "origin": "杭州",
                "destination": "三亚", "departure_date": "2099-09-18"})
        elif len(results) == 1:
            call = tool_call("get_transport_detail", {
                "candidate_id": results[0]["data"]["candidates"][0]["candidate_id"]})
        elif len(results) == 2:
            call = tool_call("search_stays", {"destination": "三亚", "check_in": "2099-09-18",
                "check_out": "2099-09-19"})
        elif len(results) == 3:
            call = tool_call("resolve_place", {"role": "arrival",
                "candidate_id": results[0]["data"]["candidates"][0]["candidate_id"]})
        elif len(results) == 4:
            call = tool_call("resolve_place", {"role": "hotel",
                "candidate_id": results[2]["data"]["candidates"][0]["candidate_id"]})
        elif len(results) == 5:
            call = tool_call("resolve_place", {"role": "meeting"})
        elif len(results) == 6:
            call = tool_call("calculate_route", {
                "origin_candidate_id": results[3]["data"]["candidates"][0]["candidate_id"],
                "destination_candidate_id": results[4]["data"]["candidates"][0]["candidate_id"]})
        elif len(results) == 7:
            call = tool_call("calculate_route", {
                "origin_candidate_id": results[4]["data"]["candidates"][0]["candidate_id"],
                "destination_candidate_id": results[5]["data"]["candidates"][0]["candidate_id"]})
        else:
            transport = results[0]["data"]["candidates"][0]
            hotel = results[2]["data"]["candidates"][0]
            arrival_route = results[6]["data"]["paths"][0]
            meeting_route = results[7]["data"]["paths"][0]
            arguments = {"status": "planned", "reason": "feasible",
                "segment_ids": [transport["candidate_id"], arrival_route["route_id"],
                                hotel["candidate_id"], meeting_route["route_id"]],
                "evidence_ids": [ref for result in results for ref in result["evidence_refs"]],
                "planning": {"hotel_departure_at": self.departure,
                             "arrival_buffer_minutes": self.buffer}}
            if self.finish_mutation:
                self.finish_mutation(arguments)
            call = tool_call("finish", arguments)
        return {"message": call, "input_tokens": 0, "output_tokens": 0}


class HumanSelectedItineraryModel(CompleteItineraryModel):
    def chat(self, messages, schemas, timeout):
        results = [json.loads(message["content"]) for message in messages
                   if message.get("role") == "tool"]
        if len(results) == 7:
            reply = next(json.loads(message["content"].split(": ", 1)[1])["human_response"]
                         for message in messages if message.get("role") == "user"
                         and message.get("content", "").startswith("Application human context: "))
            meeting_place_id = reply["option_id"]
            return {"message": tool_call("calculate_route", {
                "origin_candidate_id": results[4]["data"]["candidates"][0]["candidate_id"],
                "destination_candidate_id": meeting_place_id}), "input_tokens": 0, "output_tokens": 0}
        return super().chat(messages, schemas, timeout)


def itinerary_supplier(request):
    response = route_supplier(request)
    if request.method == "GET" and request.url.path.endswith("/geo"):
        payload = response.json()
        address = request.url.params["address"]
        if "机场路" in address:
            payload["geocodes"][0]["location"] = "109.1000,18.3000"
        elif "凤凰路" in address:
            payload["geocodes"][0]["location"] = "109.1200,18.3200"
        return httpx.Response(response.status_code, json=payload)
    if request.method == "GET" and request.url.path.endswith("/driving"):
        payload = response.json()
        payload["route"].pop("taxi_cost", None)
        return httpx.Response(response.status_code, json=payload)
    return response


def ambiguous_meeting_supplier(request):
    response = itinerary_supplier(request)
    if (request.method == "GET" and request.url.path.endswith("/text")
            and request.url.params["keywords"] == MEETING_REQUEST["meeting_location"]):
        payload = response.json()
        second = {**payload["pois"][0], "id": "meeting-b", "location": "109.1210,18.3210"}
        payload["pois"].append(second)
        return httpx.Response(response.status_code, json=payload)
    return response


def _trace_path(temp_dir, name):
    retained_dir = os.environ.get("TRAVEL_ISSUE_8_TRACE_DIR")
    if not retained_dir:
        return Path(temp_dir) / "trace.sqlite3"
    path = Path(retained_dir) / f"{name}.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


class RealItineraryTests(unittest.TestCase):
    def test_plan_trip_presents_evidence_bound_on_time_itinerary(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {
                "AMAP_API_KEY": "amap-secret", "TUNIU_API_KEY": "tuniu-secret"}):
            trace_path = _trace_path(directory, "complete-real-itinerary")
            run = plan_trip({"id": "complete-real-itinerary", "request": MEETING_REQUEST},
                CompleteItineraryModel(), Store(trace_path),
                tool_mode="real", transport_http=httpx.MockTransport(itinerary_supplier))

        if os.environ.get("TRAVEL_ISSUE_8_TRACE_DIR"):
            print(f"issue8_trace_id={run['trace_id']} sqlite={trace_path}")
        self.assertEqual((run["termination"], run["status"]), ("completed", "planned"))
        self.assertEqual(run["verdict"]["itinerary"][3]["estimated_arrival_at"],
                         "2099-09-19T08:15:42+08:00")
        self.assertEqual(run["verdict"]["itinerary"][3]["buffered_arrival_at"],
                         "2099-09-19T08:45:42+08:00")
        self.assertIsNone(run["verdict"]["itinerary"][1]["route"]["taxi_fare_cny"])
        self.assertIsNone(run["verdict"]["itinerary"][3]["route"]["taxi_fare_cny"])
        self.assertIsNone(run["verdict"]["total_cny"])
        self.assertIn("2099-09-18 16:05", run["draft"])
        self.assertIn("2099-09-18 18:55", run["draft"])
        self.assertIn("2099-09-19 08:00", run["draft"])
        self.assertIn("08:15:42", run["draft"])
        self.assertIn("费用未知", run["draft"])
        self.assertIn("杭州", run["draft"])
        for evidence_id in run["verdict"]["required_evidence_ids"]:
            self.assertIn(evidence_id, run["draft"])

    def test_plan_trip_rejects_arrival_one_minute_late(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {
                "AMAP_API_KEY": "amap-secret", "TUNIU_API_KEY": "tuniu-secret"}):
            run = plan_trip({"id": "late-real-itinerary", "request": MEETING_REQUEST},
                CompleteItineraryModel(departure="2099-09-19T08:45:18+08:00", buffer=0),
                Store(Path(directory) / "trace.sqlite3"), tool_mode="real",
                transport_http=httpx.MockTransport(itinerary_supplier))

        self.assertEqual((run["termination"], run["status"]), ("invalid_response", "needs_review"))
        self.assertEqual(run["guardrail_blocks"], 1)

    def test_plan_trip_rejects_disconnected_route_segments(self):
        def swap_routes(arguments):
            arguments["segment_ids"][1], arguments["segment_ids"][3] = (
                arguments["segment_ids"][3], arguments["segment_ids"][1])

        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {
                "AMAP_API_KEY": "amap-secret", "TUNIU_API_KEY": "tuniu-secret"}):
            run = plan_trip({"id": "disconnected-real-itinerary", "request": MEETING_REQUEST},
                CompleteItineraryModel(finish_mutation=swap_routes),
                Store(Path(directory) / "trace.sqlite3"), tool_mode="real",
                transport_http=httpx.MockTransport(itinerary_supplier))

        self.assertEqual((run["termination"], run["status"]), ("invalid_response", "needs_review"))

    def test_plan_trip_rejects_unobserved_evidence(self):
        def add_unobserved_evidence(arguments):
            arguments["evidence_ids"].append("forged:evidence")

        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {
                "AMAP_API_KEY": "amap-secret", "TUNIU_API_KEY": "tuniu-secret"}):
            run = plan_trip({"id": "unobserved-real-itinerary", "request": MEETING_REQUEST},
                CompleteItineraryModel(finish_mutation=add_unobserved_evidence),
                Store(Path(directory) / "trace.sqlite3"), tool_mode="real",
                transport_http=httpx.MockTransport(itinerary_supplier))

        self.assertEqual((run["termination"], run["status"]), ("invalid_response", "needs_review"))

    def test_plan_trip_rejects_unavailable_claim_when_observed_candidates_can_meet_request(self):
        def claim_unavailable(arguments):
            arguments.update(status="unavailable", reason="no_feasible_route", segment_ids=[])
            arguments.pop("planning")

        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {
                "AMAP_API_KEY": "amap-secret", "TUNIU_API_KEY": "tuniu-secret"}):
            run = plan_trip({"id": "unsupported-unavailable", "request": MEETING_REQUEST},
                CompleteItineraryModel(finish_mutation=claim_unavailable),
                Store(Path(directory) / "trace.sqlite3"), tool_mode="real",
                transport_http=httpx.MockTransport(itinerary_supplier))

        self.assertEqual((run["termination"], run["status"]), ("invalid_response", "needs_review"))

    def test_plan_trip_rejects_supplier_transport_arrival_on_meeting_day(self):
        def next_day_supplier(request):
            response = itinerary_supplier(request)
            if (request.method == "POST"
                    and json.loads(request.content)["params"]["name"] == "searchLowestPriceFlight"):
                payload = response.json()
                payload["result"]["structuredContent"]["data"][0]["arrivalTime"] = "2099-09-19 00:30:00"
                return httpx.Response(response.status_code, json=payload)
            return response

        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {
                "AMAP_API_KEY": "amap-secret", "TUNIU_API_KEY": "tuniu-secret"}):
            run = plan_trip({"id": "next-day-arrival", "request": MEETING_REQUEST},
                CompleteItineraryModel(), Store(Path(directory) / "trace.sqlite3"),
                tool_mode="real", transport_http=httpx.MockTransport(next_day_supplier))

        self.assertEqual((run["termination"], run["status"]), ("invalid_response", "needs_review"))

    def test_plan_trip_requires_human_selection_for_ambiguous_meeting_place(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {
                "AMAP_API_KEY": "amap-secret", "TUNIU_API_KEY": "tuniu-secret"}):
            run = plan_trip({"id": "ambiguous-meeting-place", "request": MEETING_REQUEST},
                CompleteItineraryModel(), Store(Path(directory) / "trace.sqlite3"),
                tool_mode="real", transport_http=httpx.MockTransport(ambiguous_meeting_supplier))

        self.assertEqual((run["termination"], run["status"]), ("invalid_response", "needs_review"))

    def test_hil_selected_meeting_place_remains_bound_through_final_plan(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {
                "AMAP_API_KEY": "amap-secret", "TUNIU_API_KEY": "tuniu-secret"}):
            store = Store(Path(directory) / "trace.sqlite3")
            waiting = hil.start_trip({"id": "meeting-place-hil", "request": MEETING_REQUEST},
                HumanSelectedItineraryModel(), store, tool_mode="real", confirm_request=False,
                transport_http=httpx.MockTransport(ambiguous_meeting_supplier))
            self.assertEqual(waiting["execution_status"], "waiting_for_human")
            selected = waiting["human_request"]["options"][1]["option_id"]
            done = hil.respond_trip(waiting["run_id"], Store(store.path),
                lambda _: HumanSelectedItineraryModel(),
                interaction_id=waiting["human_request"]["interaction_id"],
                state_version=waiting["state_version"], response_id="meeting-choice-b",
                reply={"option_id": selected}, transport_http=httpx.MockTransport(ambiguous_meeting_supplier))

        self.assertEqual(done["result"]["termination"], "completed")
        self.assertEqual(done["result"]["status"], "planned")
        route = next(result for result in done["result"]["tool_results"]
                     if result["tool"] == "calculate_route"
                     and result["data"]["destination"]["role"] == "meeting")
        self.assertEqual(route["data"]["destination_candidate_id"], selected)


if __name__ == "__main__":
    unittest.main()
