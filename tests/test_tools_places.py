"""Observed candidates through real place and route adapters, with synthetic HTTP."""
import json
from pathlib import Path
import unittest

import httpx
from test_tools_contract import ToolsCase
from suppliers import REQUEST, STAY, supplier


class PlacesAndRoutes(ToolsCase):
    def prepare_places(self):
        flight = self.candidate()
        self.call("get_transport_detail", {"candidate_id": flight})
        arrival = self.candidate("resolve_place", {"role": "arrival", "candidate_id": flight})
        hotel = self.candidate("search_stays", STAY)
        stay = self.candidate("resolve_place", {"role": "hotel", "candidate_id": hotel})
        meeting = self.candidate("resolve_place", {"role": "meeting"})
        return arrival, stay, meeting

    def test_tool_chain_preserves_independent_route_evidence_and_unknown_cost(self):
        arrival, hotel, meeting = self.prepare_places()
        first = self.call("calculate_route", {"origin_candidate_id": arrival, "destination_candidate_id": hotel})
        second = self.call("calculate_route", {"origin_candidate_id": hotel, "destination_candidate_id": meeting})
        self.assertEqual((first["status"], second["status"]), ("ok", "ok"))
        a, b = first["data"]["paths"][0], second["data"]["paths"][0]
        self.assertEqual(a["duration_basis"], "query_time_estimate")
        self.assertEqual(a["toll_cny"], "12")
        self.assertIsNone(b["toll_cny"])
        self.assertEqual(a["taxi_fare_cny"], "88")
        self.assertNotEqual(a["route_id"], b["route_id"])
        self.assertNotEqual(first["evidence_refs"], second["evidence_refs"])
        self.assertEqual(first["data"]["destination_candidate_id"], second["data"]["origin_candidate_id"])
        self.assertEqual(self.tools.http_requests, 14)
        files = list(Path(self.folder.name).glob("*.json"))
        self.assertTrue(files)
        text = json.dumps(self.tools.snapshot()) + "".join(p.read_text(encoding="utf-8") for p in files)
        for secret in ("test-secret", "amap-secret"):
            self.assertNotIn(secret, text)

    def test_unobserved_sources_or_changed_meeting_are_rejected_before_http(self):
        for args in ({"role": "arrival", "candidate_id": "foreign"}, {"role": "hotel", "candidate_id": "foreign"},
                     {"role": "meeting", "keyword": "其他地点"}, {"role": "meeting", "city": "海口"},
                     {"role": "meeting", "candidate_id": "foreign"}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.call("resolve_place", args)
        self.assertEqual(self.requests, [])

    def test_route_rejects_unobserved_and_wrong_role_pairs_without_http(self):
        arrival, hotel, meeting = self.prepare_places()
        count = len(self.requests)
        for a, b in (("foreign", hotel), (meeting, hotel), (arrival, meeting)):
            with self.subTest(a=a, b=b), self.assertRaises(ValueError):
                self.call("calculate_route", {"origin_candidate_id": a, "destination_candidate_id": b})
        self.assertEqual(len(self.requests), count)

    def test_place_ambiguity_and_invalid_coordinates_remain_uncertain(self):
        for location, city, count in (("109.12,18.32", "三亚市", 2), ("109.12,18.32", "海口市", 1),
                                     ("bad", "三亚市", 1), ("999,999", "三亚市", 1)):
            with self.subTest(location=location, city=city, count=count):
                def ambiguous(req):
                    if req.url.path.endswith("/text"):
                        return httpx.Response(200, json={"status": "1", "pois": [{"id": str(i),
                            "name": REQUEST["meeting_location"], "cityname": city, "location": location}
                            for i in range(count)]})
                    return supplier(req)
                self.handler = ambiguous
                result = self.call("resolve_place", {"role": "meeting"})
                self.assertEqual(result["status"], "ok")
                self.assertTrue(result["data"]["candidates"])
                self.assertTrue(all(not c["identity_confirmed"] for c in result["data"]["candidates"]))
                expected = "multiple_candidates" if count > 1 else "city_not_verified" if city == "海口市" else "invalid_coordinates"
                self.assertIn(expected, result["data"]["candidates"][0]["uncertainty_reasons"])

    def test_place_supplier_failure_redacts_error_and_preserves_history(self):
        def timeout(req):
            raise httpx.ReadTimeout("amap-secret", request=req)
        self.handler = timeout
        failed = self.call("resolve_place", {"role": "meeting"})
        self.assertEqual(failed["status"], "error")
        self.handler = supplier
        success = self.call("resolve_place", {"role": "meeting"})
        self.assertEqual(success["status"], "ok")
        self.assertEqual(success["retry_of"], failed["call_id"])
        self.assertNotIn("amap-secret", json.dumps(self.tools.snapshot()))

