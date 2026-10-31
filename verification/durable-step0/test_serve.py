"""Real loopback HTTP checks for the disposable browser fixture's origin boundary."""
import http.client
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from urllib.parse import urlsplit

from control import HERE


class LoopbackBoundary(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stderr = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
        cls.process = subprocess.Popen([sys.executable, "-B", "-X", "utf8", str(HERE / "serve.py")],
            stdout=subprocess.PIPE, stderr=cls.stderr, text=True, encoding="utf-8",
            start_new_session=os.name != "nt")
        cls.lines = queue.Queue()
        def read_lines():
            try:
                for line in cls.process.stdout:
                    cls.lines.put(line)
            finally:
                cls.lines.put("")

        cls.reader = threading.Thread(target=read_lines, daemon=True)
        cls.reader.start()
        cls.addClassCleanup(cls.close_server)
        endpoint = cls.lines.get(timeout=25).strip()
        if not endpoint.startswith("DURABLE_BROWSER "):
            raise AssertionError(endpoint)
        cls.origin = endpoint.split(" ", 1)[1]
        cls.port = urlsplit(cls.origin).port
        cls.host = f"127.0.0.1:{cls.port}"

    @classmethod
    def request(cls, method, path, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=5)
        try:
            connection.request(method, path, body="{}" if method == "POST" else None, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    @classmethod
    def close_server(cls):
        if cls.process.poll() is None:
            try:
                if hasattr(cls, "port"):
                    cls.request("POST", "/stop")
                cls.process.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                if os.name == "nt":
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(cls.process.pid)], capture_output=True)
                else:
                    import signal
                    os.killpg(cls.process.pid, signal.SIGKILL)
                cls.process.wait(timeout=5)
        cls.reader.join(timeout=2)
        cls.process.stdout.close()
        cls.stderr.close()

    def test_rebinding_host_is_rejected_for_reads_and_writes(self):
        headers = {"Host": f"attacker.invalid:{self.port}", "Origin": f"http://attacker.invalid:{self.port}"}
        for method, path in (("GET", "/state"), ("POST", "/restart")):
            with self.subTest(method=method):
                self.assertEqual(self.request(method, path, headers)[0], 403)

    def test_foreign_origin_is_rejected_even_with_loopback_host(self):
        headers = {"Host": self.host, "Origin": "https://attacker.invalid"}
        self.assertEqual(self.request("POST", "/restart", headers)[0], 403)

    def test_loopback_origin_can_restart_without_model_calls(self):
        headers = {"Host": self.host, "Origin": self.origin}
        self.assertEqual(self.request("POST", "/restart", headers)[0], 200)
        status, body = self.request("GET", "/state", headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["counts"], [0, 0])
