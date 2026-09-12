from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from travel_agent.agent import plan_trip
from travel_agent.domain import scenario_by_id
from travel_agent.providers import Baseline
from travel_agent.server import make_server
from travel_agent.telemetry import Store, make_otel


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "test.sqlite3")

    def start(self, server):
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def stop():
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            if hasattr(server, "app"):
                server.app.executor.shutdown(wait=True)
        self.addCleanup(stop)
        return f"http://127.0.0.1:{server.server_address[1]}"

    def test_dashboard_http_job_evaluation_and_trace(self):
        base = self.start(make_server(self.store, Baseline(), port=0))
        def fetch(path, body=None, client=True):
            headers = {"Content-Type": "application/json"}
            if client: headers["X-Travel-Client"] = "dashboard"
            req = urllib.request.Request(base+path, data=json.dumps(body).encode() if body is not None else None, headers=headers)
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.read()
        def wait(job):
            deadline = time.monotonic()+10
            while time.monotonic() < deadline:
                result = json.loads(fetch("/api/jobs/"+job["id"]))
                if result["status"] != "running": return result
                time.sleep(.02)
            self.fail("Job did not complete")
        html = fetch("/").decode()
        for view in ("traces-view", "evals-view", "flow-view"):
            self.assertIn(view, html)
        self.assertEqual(json.loads(fetch("/api/health"))["status"], "ok")
        self.assertEqual(len(json.loads(fetch("/api/config"))["scenarios"]), 6)
        request = scenario_by_id("hangzhou-shanghai")["request"]
        job = json.loads(fetch("/api/run", {"provider": "baseline", "request": request}))
        result = wait(job)
        self.assertEqual(result["status"], "completed")
        trace = json.loads(fetch("/api/runs/"+result["result_id"]))
        self.assertEqual(trace["verdict"]["total_cny"], 628)
        self.assertTrue(trace["spans"])
        job = json.loads(fetch("/api/evaluate", {"provider": "baseline", "split": "all", "repeats": 1}))
        result = wait(job)
        report = json.loads(fetch("/api/evaluations"))[0]
        self.assertEqual(report["id"], result["result_id"])
        self.assertTrue(report["gate_passed"])
        with self.assertRaises(urllib.error.HTTPError) as caught:
            fetch("/api/run", {"provider": "baseline"}, client=False)
        self.assertEqual(caught.exception.code, 403)
        for invalid in ("not-a-date", "0001-01-01T08:00", "9999-12-31T23:00-12:00"):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                fetch("/api/run", {"request": {**request, "meeting_at": invalid}})
            self.assertEqual(caught.exception.code, 400)

    def test_web_real_mode_runs_policy_and_rejects_baseline(self):
        from test_policy import PolicyModel, POLICY_HASH
        class Model(PolicyModel):
            name = "deepseek"
        base = self.start(make_server(self.store, Model(), port=0))
        def fetch(path, body=None):
            req = urllib.request.Request(base + path,
                data=json.dumps(body).encode() if body is not None else None,
                headers={"Content-Type": "application/json", "X-Travel-Client": "dashboard"})
            with urllib.request.urlopen(req, timeout=5) as response:
                return json.loads(response.read())
        self.assertEqual(fetch("/api/config")["real_tools"], ["get_policy_snapshot", "search_transport", "get_transport_detail", "search_stays", "get_stay_detail", "resolve_place", "calculate_route"])
        job = fetch("/api/run", {"provider": "deepseek", "tool_mode": "real", "scenario": "hangzhou-shanghai"})
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = fetch("/api/jobs/" + job["id"])
            if result["status"] != "running":
                break
            time.sleep(.02)
        self.assertEqual(result["status"], "completed")
        trace = fetch("/api/runs/" + result["result_id"])
        self.assertEqual(trace["tool_mode"], "real")
        self.assertEqual(trace["termination"], "completed")
        self.assertEqual(trace["verdict"]["reason"], "missing_data")
        self.assertEqual(trace["evidence"][0]["data"]["content_hash"], POLICY_HASH)
        for mode in ("real", "unknown"):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                fetch("/api/run", {"provider": "baseline", "tool_mode": mode})
            self.assertEqual(caught.exception.code, 400)

    def test_optional_otlp_exports_same_chain_and_trace_link(self):
        try:
            from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
        except ImportError:
            self.skipTest("Install .[otel] to validate the optional exporter")
        payloads = []
        class Collector(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                payloads.append(self.rfile.read(int(self.headers["Content-Length"])))
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()
        base = self.start(ThreadingHTTPServer(("127.0.0.1", 0), Collector))
        tracer, owner = make_otel(base+"/v1/traces")
        try:
            run = plan_trip(scenario_by_id("retry-once"), Baseline(), self.store, otel=tracer)
            self.assertTrue(owner.force_flush(timeout_millis=5000))
        finally:
            owner.shutdown()
        self.assertTrue(payloads)
        spans = []
        for payload in payloads:
            message = ExportTraceServiceRequest.FromString(payload)
            spans.extend(s for resource in message.resource_spans for scope in resource.scope_spans for s in scope.spans)
        self.assertIn("invoke_agent", [s.name for s in spans])
        self.assertIn("validate_result", [s.name for s in spans])
        self.assertTrue(any(s.status.code == 2 for s in spans))
        for span in spans:
            attrs = {a.key: a.value.string_value for a in span.attributes}
            self.assertEqual(attrs["travel.local_trace_id"], run["trace_id"])


if __name__ == "__main__":
    unittest.main()

