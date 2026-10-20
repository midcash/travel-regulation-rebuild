"""Synthetic model/supplier responses. No keys, online calls, or local history."""
from datetime import datetime
import json
import httpx

from travel_agent.facts import CHINA

NOW = datetime(2026, 10, 19, 10, 0, tzinfo=CHINA)


def model_responses(text, captured):
    if text == "我要去三亚。":
        return [{"text": "请告诉我出发城市和出行日期。"}]
    if text == "模型失败":
        return [{"error": "Synthetic model failure"}]
    if text == "你能帮我做什么？":
        return [{"text": "我可以帮你查询国内航班。告诉我出发地、目的地和日期即可。"}]
    date = "2026-10-20" if "明天" in text else "2026-11-20"
    return [{"tool": "search_flights", "args": {"origin": "杭州", "destination": "三亚", "departure_date": date}},
            {"text": "本次查询结果已展示在航班卡片中。价格未知，不代表可以免费出行。"}]


def supplier(request):
    payload = json.loads(request.content)
    assert payload["params"]["name"] == "searchLowestPriceFlight"
    date = payload["params"]["arguments"]["departureDate"]
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": {"structuredContent": {
        "data": [{"flightNumber": "OFFLINE-101", "airlineCompany": "合成航司", "departureAirport": "萧山",
                  "arrivalAirport": "凤凰", "departureTime": f"{date} 10:00:00", "arrivalTime": f"{date} 13:00:00"}]}}})
