"""Loopback-only browser inspection fixture. Always creates a new disposable store."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import time

from control import HERE, Ledger, Node


def main():
    with tempfile.TemporaryDirectory(prefix="durable-browser-") as folder:
        root = Path(folder)
        ledger = Ledger(root)
        node = Node(root, ledger)
        lock = threading.Lock()
        timer = None

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def reply(self, value, status=200):
                body = json.dumps(value, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write((HERE / "inspect.html").read_bytes())
                elif self.path == "/state":
                    with lock:
                        state = node.call("snapshot")
                        state.pop("views")
                        state.pop("publications")
                        state["counts"] = ledger.counts()
                        state["timer"] = node.call("task", taskId=timer) if timer else None
                        self.reply(state)
                else:
                    self.send_error(404)

            def do_POST(self):
                nonlocal node, timer
                expected = "http://" + self.headers.get("Host", "")
                if self.headers.get("Origin", expected) != expected:
                    self.send_error(403)
                    return
                size = int(self.headers.get("Content-Length", "0"))
                if size > 4096:
                    self.send_error(413)
                    return
                try:
                    data = json.loads(self.rfile.read(size) or "{}")
                    with lock:
                        if self.path == "/prompt":
                            result = node.call("prompt", text=data["text"], requestId=data["requestId"],
                                               scenario=data.get("scenario") or None)
                        elif self.path == "/restart":
                            node.kill()
                            node = Node(root, ledger)
                            node.call("resume")
                            node.call("idle")
                            result = {"restarted": True}
                        elif self.path == "/timer":
                            timer = node.call("timer", until=int(time.time() * 1000) + 3000)
                            node.call("idle")
                            node.call("abort")
                            result = {"taskId": timer}
                        elif self.path == "/stop":
                            result = {"stopped": True}
                            threading.Thread(target=server.shutdown, daemon=True).start()
                        else:
                            self.send_error(404)
                            return
                        self.reply(result)
                except (AssertionError, KeyError, ValueError) as error:
                    self.reply({"error": str(error)}, 400)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        print(f"DURABLE_BROWSER http://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        finally:
            server.server_close()
            node.kill()


if __name__ == "__main__":
    main()
