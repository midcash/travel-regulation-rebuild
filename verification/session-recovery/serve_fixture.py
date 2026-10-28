"""Loopback-only recovery fixture; real product HTTP + Node/Pi, synthetic peers.

Run only with an explicit, disposable --root. Never loads .env or user history.
The JSONL ledger records observed activity, independently of product persistence.
"""
from __future__ import annotations

import argparse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "verification" / "baseline"))

import httpx
import uvicorn

from fixtures import supplier
from travel_agent.app import create_app
from travel_agent.runtime import Conversation, NodeWorker

PRIVATE_REASONING = "RECOVERY_FIXTURE_PRIVATE_REASONING"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--clock", default="2026-10-19T10:00:00+08:00")
    args = parser.parse_args()
    folder = args.root.resolve()
    folder.mkdir(parents=True, exist_ok=True)
    ledger_lock = threading.Lock()

    def record(kind, **values):
        with ledger_lock, (folder / "activity.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"kind": kind, **values}, ensure_ascii=False) + "\n")
            stream.flush()

    def wait_gate(name):
        # Only synthetic peers wait here; the application and event loop stay real.
        marker = folder / ("hold-" + name)
        if marker.exists():
            record("gate", name=name)
            deadline = time.monotonic() + 40
            while marker.exists() and time.monotonic() < deadline:
                time.sleep(.02)

    def content(message):
        value = message.get("content", "")
        if isinstance(value, str):
            return value
        return "".join(block.get("text", "") for block in (value or []) if isinstance(block, dict))

    class Model(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            if self.path != "/chat/completions":
                self.send_error(404)
                return
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            messages = body["messages"]
            record("model", messages=messages)
            wait_gate("model")
            last_user = next(content(m) for m in reversed(messages) if m["role"] == "user")
            if "模型失败" in last_user:
                self.send_error(503, "Synthetic model failure")
                return
            tool_messages = [m for m in messages if m["role"] == "tool"]
            query = "杭州" in last_user or "明天" in last_user
            if query and messages[-1]["role"] != "tool":
                # Deliberately require restored history for a partial follow-up.
                if "三亚" not in "".join(content(m) for m in messages if m["role"] == "user"):
                    delta = {"content": "缺少历史目的地，无法查询。"}
                else:
                    date = (datetime.fromisoformat(last_user.split("：", 1)[1].split("；", 1)[0]).date()
                            if "明天" in last_user else None)
                    if date:
                        from datetime import timedelta
                        departure = (date + timedelta(days=1)).isoformat()
                    else:
                        departure = "2026-11-20"
                    delta = {"tool_calls": [{"index": 0, "id": "fixture-call-" + str(len(messages)),
                        "type": "function", "function": {"name": "search_flights", "arguments": json.dumps({
                            "origin": "杭州", "destination": "三亚", "departure_date": departure}, ensure_ascii=False)}}]}
            elif messages[-1]["role"] == "tool":
                wait_gate("after-tool")
                delta = {"content": "本次查询取得 OFFLINE-101，价格未知。历史结果不代表当前报价。"}
            elif "刚才查到" in last_user:
                observed = "OFFLINE-101" in "".join(content(m) for m in tool_messages)
                delta = {"content": "历史查询取得 OFFLINE-101，价格未知。" if observed else "缺少历史工具结果。"}
            elif "我要去三亚" in last_user:
                delta = {"content": "请告诉我出发城市和出行日期。"}
            else:
                delta = {"content": "我可以继续帮你查询国内航班。"}
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            finish = "tool_calls" if "tool_calls" in delta else "stop"
            for update, reason in [({"role": "assistant", "reasoning_content": PRIVATE_REASONING}, None),
                                   (delta, None), ({}, finish)]:
                chunk = {"id": "offline", "object": "chat.completion.chunk", "created": 0,
                    "model": "deepseek-v4-flash", "choices": [{"index": 0, "delta": update, "finish_reason": reason}]}
                try:
                    self.wfile.write(("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode())
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    return
            self.wfile.write(b"data: [DONE]\n\n")

    model = ThreadingHTTPServer(("127.0.0.1", 0), Model)
    model.daemon_threads = True
    threading.Thread(target=model.serve_forever, daemon=True).start()
    # The runtime's actual OpenAI-compatible adapter connects only to this peer.
    os.environ.update({"DEEPSEEK_API_KEY": "recovery-fixture-synthetic-key",
        "DEEPSEEK_BASE_URL": f"http://127.0.0.1:{model.server_port}",
        "DEEPSEEK_MODEL": "deepseek-v4-flash", "TUNIU_API_KEY": "synthetic",
        "TUNIU_FLIGHT_URL": "https://supplier.invalid/flight",
        "TRAVEL_SESSION_DB": str(folder / "sessions.sqlite3")})

    class ObservedWorker(NodeWorker):
        async def start(self, **kwargs):
            result = await super().start(**kwargs)
            record("worker", pid=self.process.pid)
            return result

    def synthetic_supplier(request):
        record("supplier", body=json.loads(request.content))
        wait_gate("supplier")
        if (folder / "fail-supplier").exists():
            return httpx.Response(503)
        return supplier(request)

    conversation = Conversation(clock=lambda: datetime.fromisoformat(args.clock),
        transport_http=httpx.MockTransport(synthetic_supplier), evidence_root=folder / "evidence",
        worker_factory=ObservedWorker)
    # Preserve the production create_app startup, reset, SSE and shutdown paths.
    app = create_app(conversation)

    @app.get("/__verification__/counts")
    def counts():
        records = [json.loads(line) for line in (folder / "activity.jsonl").read_text(encoding="utf-8").splitlines()]
        return {kind: sum(r["kind"] == kind for r in records) for kind in ("model", "supplier")}

    # Pre-bind a free port; avoid port-release races between parent and child.
    import socket
    sock = socket.socket()
    sock.bind(("127.0.0.1", args.port))
    port = sock.getsockname()[1]
    (folder / "endpoint.json").write_text(json.dumps({"port": port}), encoding="utf-8")
    (folder / "activity.jsonl").touch(exist_ok=True)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", timeout_graceful_shutdown=2)
    with patch("travel_agent.app.load_env"):
        uvicorn.Server(config).run(sockets=[sock])


if __name__ == "__main__":
    main()
