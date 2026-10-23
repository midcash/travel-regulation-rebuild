"""Observe actual Pi and supplier activity through the browser fixture only."""
import asyncio
import os
import tempfile
import unittest
from unittest.mock import patch

import httpx

from serve_offline import create_offline_app
from travel_agent.app import create_app


class OfflineFixture(unittest.IsolatedAsyncioTestCase):
    async def test_counts_follow_actual_calls_and_survive_refresh_and_reset(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
                "TUNIU_API_KEY": "synthetic", "TUNIU_FLIGHT_URL": "https://supplier.invalid/flight"}), \
                patch("travel_agent.app.load_env"):
            app = create_offline_app(folder)
            async with app.router.lifespan_context(app), httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                async def counts():
                    response = await client.get("/__verification__/counts")
                    self.assertEqual(response.status_code, 200)
                    return response.json()

                async def turn(text):
                    self.assertEqual((await client.post("/api/messages", json={"text": text})).status_code, 202)
                    await asyncio.wait_for(app.state.conversation.task, 30)

                initial = await counts()
                self.assertEqual((initial["model_calls"], initial["supplier_requests"]), (0, 0))
                await turn("我要去三亚。")
                first = await counts()
                self.assertEqual((first["model_calls"], first["supplier_requests"]), (1, 0))
                await turn("杭州，2026 年 11 月 20 日。")
                second = await counts()
                self.assertEqual((second["model_calls"], second["supplier_requests"]), (3, 1))
                before = (await client.get("/api/session")).json()
                self.assertIsNone(before["messages"][-1]["results"][0]["data"]["candidates"][0]["price_cny"])
                self.assertEqual((await client.get("/")).status_code, 200)
                self.assertEqual((await client.get("/api/session")).json(), before)
                self.assertEqual(await counts(), second)
                self.assertEqual((await client.post("/api/session/reset")).status_code, 200)
                reset = await counts()
                self.assertNotEqual(reset["session_id"], second["session_id"])
                self.assertEqual((reset["model_calls"], reset["supplier_requests"]), (3, 1))
                await turn("你能帮我做什么？")
                final = await counts()
                self.assertEqual((final["model_calls"], final["supplier_requests"]), (4, 1))
                self.assertEqual(final["mode"], "synthetic")

    async def test_product_does_not_expose_fixture_counts(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://test") as client:
            self.assertEqual((await client.get("/__verification__/counts")).status_code, 404)
