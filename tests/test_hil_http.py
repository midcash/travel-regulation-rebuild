import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from travel_agent.domain import scenario_by_id
from travel_agent.providers import Baseline
from travel_agent.server import make_server
from travel_agent.telemetry import Store


class HilHttpTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "runs.db"
        self.start()

    def start(self):
        self.server = make_server(Store(self.path), Baseline(), port=0)
        server = self.server
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def stop():
            server.shutdown(); server.server_close(); thread.join(timeout=5)
            server.app.executor.shutdown(wait=True)
        self.addCleanup(stop)
        self.base = "http://127.0.0.1:" + str(server.server_address[1])

    def api(self, path, body=None, headers=None):
        req = urllib.request.Request(self.base + path,
            data=None if body is None else json.dumps(body).encode(),
            headers=headers or {"Content-Type": "application/json", "X-Travel-Client": "dashboard"})
        with urllib.request.urlopen(req, timeout=5) as result:
            return json.load(result)

    def wait(self, job):
        end = time.monotonic() + 10
        while time.monotonic() < end:
            result = self.api("/api/jobs/" + job["id"])
            if result["status"] != "running":
                return result
            time.sleep(.02)
        self.fail("Job did not finish")

    def test_wait_releases_worker_and_restart_can_reply(self):
        request = {**scenario_by_id("hangzhou-shanghai")["request"], "meeting_at": "2099-09-12T08:00+08:00"}
        job = self.wait(self.api("/api/hil/runs", {"provider": "baseline", "request": request}))
        self.assertEqual(job["execution_status"], "waiting_for_human")
        self.assertFalse(self.server.app.busy)
        run_id = job["result_id"]
        waiting = self.api("/api/hil/runs/" + run_id)
        legacy = self.wait(self.api("/api/run", {"provider": "baseline"}))
        self.assertEqual(legacy["status"], "completed")
        self.server.shutdown(); self.server.server_close(); self.server.app.executor.shutdown(wait=True)
        self.start()
        self.assertEqual(self.api("/api/hil/runs/" + run_id), waiting)
        body = {"interaction_id": waiting["human_request"]["interaction_id"], "state_version": waiting["state_version"],
                "response_id": "from-browser", "reply": {"confirmed": True}}
        result = self.wait(self.api("/api/hil/runs/" + run_id + "/respond", body))
        self.assertEqual(result["execution_status"], "completed")
        final = self.api("/api/hil/runs/" + run_id)
        self.assertEqual((final["result"]["status"], final["result"]["tool_calls"]), ("planned", 4))
        public = self.api("/api/runs/" + run_id)
        self.assertNotIn("messages", public)
        self.assertNotIn("responses", public)
        self.assertTrue(all(s["trace_id"] == run_id for s in public["spans"]))

    def test_hil_keeps_same_origin_and_size_guards(self):
        for headers, expected in (({"Content-Type": "application/json"}, 403),
                                  ({"Content-Type": "text/plain", "X-Travel-Client": "dashboard"}, 415)):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.api("/api/hil/runs", {}, headers)
            self.assertEqual(error.exception.code, expected)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.api("/api/hil/runs", {"large": "x" * 9000})
        self.assertEqual(error.exception.code, 400)
