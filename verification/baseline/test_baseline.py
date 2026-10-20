"""White-box lifecycle + black-box HTTP tests against real Node/Pi and fake providers."""
import asyncio
import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx

from travel_agent.app import create_app
from travel_agent.runtime import Conversation, NodeWorker
from fixtures import NOW, model_responses, supplier


class RecordingWorker(NodeWorker):
    def __init__(self):
        super().__init__()
        self.sent = []
        self.received = []

    async def send(self, op, **body):
        self.sent.append((op, body))
        return await super().send(op, **body)

    async def receive(self):
        event = await super().receive()
        self.received.append(event)
        return event


class Baseline(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {"TUNIU_API_KEY": "synthetic", "TUNIU_FLIGHT_URL": "https://supplier.invalid/flight"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.handler = supplier
        self.requests = []

        def http(request):
            self.requests.append(request)
            return self.handler(request)

        self.conversation = Conversation(clock=lambda: NOW, fake=True, responses=model_responses,
            transport_http=httpx.MockTransport(http), evidence_root=self.temp.name, worker_factory=RecordingWorker)
        self.app = create_app(self.conversation)
        self.life = self.app.router.lifespan_context(self.app)
        await self.life.__aenter__()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")

    async def asyncTearDown(self):
        worker = self.app.state.conversation.worker
        await self.client.aclose()
        await self.life.__aexit__(None, None, None)
        if worker:
            self.assertIsNotNone(worker.process.returncode)

    async def turn(self, text):
        response = await self.client.post("/api/messages", json={"text": text})
        self.assertEqual(response.status_code, 202, response.text)
        await asyncio.wait_for(self.conversation.task, 30)
        return (await self.client.get("/api/session")).json()

    async def wait_for_tool(self):
        async with asyncio.timeout(30):
            while not self.requests:
                await asyncio.sleep(.01)

    async def test_two_turn_query_refresh_date_facts_and_empty_failure(self):
        page = await self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("行旅", page.text)
        for name in ("style.css", "app.js"):
            self.assertEqual((await self.client.get("/static/" + name)).status_code, 200)
        first = await self.turn("我要去三亚。")
        self.assertEqual(len(self.requests), 0)
        self.assertIn("出发城市", first["messages"][-1]["text"])
        worker = self.conversation.worker
        pid = worker.process.pid
        second = await self.turn("杭州，2026 年 11 月 20 日。")
        self.assertEqual(second["run"]["status"], "complete")
        self.assertEqual(worker.process.pid, pid)
        self.assertEqual(self.conversation.tools.request, None)  # No fabricated meeting context.
        candidate = second["run"]["results"][0]["data"]["candidates"][0]
        self.assertEqual(candidate["flight_no"], "OFFLINE-101")
        self.assertIsNone(candidate["price_cny"])
        self.assertTrue(candidate["meets_request"])
        self.assertTrue(candidate["evidence_ref"])
        refresh = (await self.client.get("/api/session")).json()
        self.assertEqual(refresh, second)
        relative = await self.turn("查杭州明天去三亚的航班")
        self.assertEqual(relative["run"]["results"][0]["data"]["departure_date"], "2026-10-20")
        self.assertIn("2026-10-19", [body for op, body in worker.sent if op == "prompt"][-1]["text"])
        self.assertIn("Asia/Shanghai", [body for op, body in worker.sent if op == "prompt"][-1]["text"])
        self.assertEqual(json.loads(self.requests[-1].content)["params"]["arguments"]["departureDate"], "2026-10-20")
        self.assertNotIn("wire_text", json.dumps(relative))
        self.assertNotIn('"thinking"', json.dumps(relative))

        self.handler = lambda r: httpx.Response(200, json={"jsonrpc": "2.0", "id": json.loads(r.content)["id"], "result": {"structuredContent": {"data": []}}})
        self.assertEqual((await self.turn("杭州三亚 2026-11-20"))["run"]["status"], "empty")
        self.handler = lambda r: httpx.Response(503)
        failed = await self.turn("杭州三亚 2026-11-20")
        self.assertEqual(failed["run"]["status"], "supplier_error")
        self.assertEqual(failed["run"]["results"][0]["error"]["type"], "SupplierHTTPError")
        self.assertEqual((await self.turn("模型失败"))["run"]["status"], "model_error")

    async def test_cancel_rejects_concurrency_and_drains_supplier_without_new_model(self):
        def delayed(request):
            time.sleep(.6)
            return supplier(request)
        self.handler = delayed
        self.assertEqual((await self.client.post("/api/messages", json={"text": "杭州三亚 2026-11-20"})).status_code, 202)
        duplicate = await self.client.post("/api/messages", json={"text": "不应进入会话"})
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual((await self.client.post("/api/session/reset")).status_code, 409)
        await self.wait_for_tool()
        worker = self.conversation.worker
        await self.client.post("/api/cancel")
        await asyncio.wait_for(self.conversation.task, 10)
        state = self.conversation.snapshot()
        self.assertEqual(state["run"]["status"], "cancelled")
        self.assertEqual(state["run"]["results"], [])
        self.assertEqual(len([e for e in worker.received if e["type"] == "model_start"]), 1)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(len(state["messages"]), 2)
        self.assertIsNone(worker.process.returncode)
        self.assertEqual((await self.turn("你能帮我做什么？"))["run"]["status"], "complete")

    async def test_timeout_and_worker_crash_preserve_explicit_context_loss(self):
        # Start first so this deadline exercises an active tool, not SDK startup.
        await self.turn("我要去三亚。")
        self.conversation.timeout = .2
        def delayed(request):
            time.sleep(.4)
            return supplier(request)
        self.handler = delayed
        result = await self.turn("杭州三亚 2026-11-20")
        self.assertEqual(result["run"]["status"], "timeout")
        self.assertEqual(result["run"]["results"], [])
        self.conversation.timeout = 90
        await self.client.post("/api/messages", json={"text": "杭州三亚 2026-11-20"})
        async with asyncio.timeout(10):
            while self.conversation.run["stage"] != "正在查询航班":
                await asyncio.sleep(.01)
        worker = self.conversation.worker
        worker.process.kill()
        await asyncio.wait_for(self.conversation.task, 10)
        self.assertEqual(self.conversation.run["status"], "worker_error")
        self.assertTrue(self.conversation.context_lost)
        self.assertIsNotNone(worker.process.returncode)
        self.assertEqual((await self.client.post("/api/messages", json={"text": "继续"})).status_code, 409)
        reset = await self.client.post("/api/session/reset")
        self.assertEqual(reset.status_code, 200)
        self.assertEqual(reset.json()["messages"], [])
        self.assertFalse(reset.json()["context_lost"])

    async def test_blank_input_and_server_validation(self):
        self.assertEqual((await self.client.post("/api/messages", json={"text": " "})).status_code, 422)
        self.assertEqual((await self.client.post("/api/messages", json={"text": ""})).status_code, 422)
        for event in ({"name": "search_stays", "args": {}},
                      {"name": "search_flights", "args": {"origin": "杭州", "destination": "三亚", "departure_date": "2026-10-18"}},
                      {"name": "search_flights", "args": {"mode": "train"}}):
            self.assertEqual(self.conversation._execute(event, NOW, 45)["status"], "error")
        self.assertEqual(self.requests, [])

    async def test_local_failures_are_distinct_from_actual_supplier_failure(self):
        self.conversation.responses = lambda text, captured: [
            {"tool": "search_flights", "args": {"origin": "杭州", "destination": "三亚", "departure_date": "2026-10-18"}},
            {"text": "请提供有效的未来日期。"}]
        invalid = await self.turn("验证过去日期")
        self.assertEqual(invalid["run"]["status"], "invalid_query")
        self.assertEqual(invalid["run"]["stage"], "查询参数需要补充或修正")
        self.assertNotIn("source", invalid["run"]["results"][0])
        self.assertNotIn("observed_at", invalid["run"]["results"][0])

        self.conversation.responses = model_responses
        with patch.object(self.conversation.tools, "execute", side_effect=RuntimeError("synthetic local error")):
            local = await self.turn("杭州三亚 2026-11-20")
        self.assertEqual(local["run"]["status"], "tool_error")
        self.assertEqual(local["run"]["stage"], "本地工具执行失败")
        self.assertNotIn("source", local["run"]["results"][0])

        with patch.dict(os.environ, {"TUNIU_API_KEY": ""}):
            config = await self.turn("杭州三亚 2026-11-20")
        self.assertEqual(config["run"]["status"], "configuration_error")
        self.assertEqual(config["run"]["stage"], "查询配置错误")
        self.assertNotIn("attempted_at", config["run"]["results"][0])
        self.assertEqual(self.requests, [])

        self.handler = lambda r: httpx.Response(503)
        failed = await self.turn("杭州三亚 2026-11-20")
        self.assertEqual(failed["run"]["status"], "supplier_error")
        self.assertEqual(failed["run"]["stage"], "供应商查询失败")
        result = failed["run"]["results"][0]
        self.assertTrue(result["evidence_refs"])
        self.assertIn("attempted_at", result)
        self.assertNotIn("observed_at", result)
