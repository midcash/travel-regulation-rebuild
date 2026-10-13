from datetime import date, datetime
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from travel_agent.domain import CHINA, normalize_request
from travel_agent.providers import tool_call
from travel_agent.real_tools import RealTools
from travel_agent.request_parser import parse_natural_request
from travel_agent.server import make_server
from travel_agent.telemetry import Store


REQUEST_TEXT = "下周六早上 9 点在三亚湾红树林国际会议中心开会，从杭州出发，提前一晚抵达，乘飞机并住一晚。"
TEST_NOW = datetime(2030, 9, 12, 9, tzinfo=CHINA)


class MissingDataProvider:
    name = "deepseek"
    model = "request-parser-test"

    def __init__(self):
        self.calls = 0

    def chat(self, messages, schemas, timeout):
        self.calls += 1
        return {"message": tool_call("finish", {"status": "needs_review", "reason": "missing_data",
                                                 "segment_ids": [], "evidence_ids": []}),
                "input_tokens": 0, "output_tokens": 0}


class RequestParserTests(unittest.TestCase):
    def test_resolves_relative_meeting_date_and_preferences_from_fixed_clock(self):
        parsed = parse_natural_request(REQUEST_TEXT, TEST_NOW)
        request = normalize_request(parsed, tool_mode="real")

        self.assertEqual(request["origin"], "杭州")
        self.assertEqual(request["destination"], "三亚")
        self.assertEqual(request["meeting_location"], "三亚湾红树林国际会议中心")
        self.assertEqual(request["meeting_at"], "2030-09-21T09:00+08:00")
        self.assertEqual(request["transport_mode"], "flight")
        self.assertTrue(request["arrive_previous_day"])
        self.assertEqual((request["required_arrival_date"], request["stay_nights"]), ("2030-09-20", 1))

    def test_page_submission_uses_one_clock_capture_and_saves_resolved_request_to_trace(self):
        calls = []
        provider = MissingDataProvider()
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "traces.sqlite3")
            server = make_server(store, provider, port=0, clock=lambda: calls.append(TEST_NOW) or TEST_NOW)
            self._serve(server)
            job = self._post("/api/run", {"provider": "deepseek", "tool_mode": "real",
                                           "request_text": REQUEST_TEXT})
            self.assertIn("2030-09-21 09:00", job["request_summary"])
            self.assertIn("飞机", job["request_summary"])
            self.assertIn("1 人 1 间房", job["request_summary"])
            self.assertEqual(len(calls), 1)

            result = self._wait(job)
            trace = self._get("/api/runs/" + result["result_id"])
            self.assertEqual(trace["request"]["meeting_at"], "2030-09-21T09:00+08:00")
            self.assertEqual(trace["request"]["required_arrival_date"], "2030-09-20")
            self.assertEqual(trace["request"]["transport_mode"], "flight")
            self.assertEqual(trace["request_received_at"], "2030-09-12T09:00:00+08:00")
            self.assertEqual(trace["http_requests"], 0)
            self.assertEqual(trace["verdict"]["reason"], "missing_data")
            self.assertEqual(provider.calls, 1)

    def test_past_meeting_date_is_rejected_before_model_or_supplier_calls(self):
        calls = []
        provider = MissingDataProvider()
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "traces.sqlite3")
            server = make_server(store, provider, port=0, clock=lambda: calls.append(TEST_NOW) or TEST_NOW)
            self._serve(server)
            stale = "2020-09-11 09:00 在三亚湾红树林国际会议中心开会，从杭州出发，提前一晚抵达，乘飞机并住一晚。"
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self._post("/api/run", {"provider": "deepseek", "tool_mode": "real", "request_text": stale})
            self.assertEqual(caught.exception.code, 400)
            self.assertIn("请更正日期", caught.exception.read().decode())
            self.assertEqual(len(calls), 1)
            self.assertEqual(provider.calls, 0)
            self.assertEqual(store.list_runs(), [])

    def test_real_tools_reject_a_past_supplier_date_before_http(self):
        tool = RealTools("stale-date", request={}, current_date=date(2030, 9, 12))
        with self.assertRaisesRegex(ValueError, "captured request date"):
            tool.execute("search_transport", {"mode": "flight", "origin": "杭州", "destination": "三亚",
                                               "departure_date": "2030-09-11"})
        with self.assertRaisesRegex(ValueError, "captured request date"):
            tool.execute("search_stays", {"destination": "三亚", "check_in": "2030-09-11",
                                           "check_out": "2030-09-12"})
        self.assertEqual(tool.http_requests, 0)

    def _serve(self, server):
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: self._stop(server, thread))
        self.base = f"http://127.0.0.1:{server.server_address[1]}"

    @staticmethod
    def _stop(server, thread):
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        server.app.executor.shutdown(wait=True)

    def _request(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as response:
            return json.loads(response.read())

    def _post(self, path, body):
        request = urllib.request.Request(self.base + path, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "X-Travel-Client": "dashboard"})
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read())

    def _get(self, path):
        return self._request(path)

    def _wait(self, job):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = self._request("/api/jobs/" + job["id"])
            if result["status"] != "running":
                return result
            time.sleep(0.02)
        self.fail("Planning job did not finish")


if __name__ == "__main__":
    unittest.main()
