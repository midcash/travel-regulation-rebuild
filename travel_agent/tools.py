from __future__ import annotations

import copy
from datetime import datetime, timedelta
import json
from .domain import PACKAGE

TOOL_NAMES = ("get_policy", "search_intercity", "search_hotels", "search_local_transport")


def function(name, description, properties, required=()):
    return {"type": "function", "function": {"name": name, "description": description,
        "parameters": {"type": "object", "properties": properties,
                       "required": list(required), "additionalProperties": False}}}


SCHEMAS = [function(name, description, {}) for name, description in zip(TOOL_NAMES, (
    "读取当前请求的公司演示差旅政策。", "查询当前请求前一晚的模拟城际交通。",
    "查询当前目的地一晚的模拟酒店。", "查询车站到酒店及次日酒店到会议的模拟市内交通。"))] + [
    function("finish", "提交一条有证据的会前到达方案，或明确说明没有方案/资料不足。", {
        "status": {"type": "string", "enum": ["planned", "unavailable", "needs_review"]},
        "reason": {"type": "string", "enum": ["feasible", "no_feasible_route", "missing_data"]},
        "segment_ids": {"type": "array", "items": {"type": "string"}},
        "evidence_ids": {"type": "array", "items": {"type": "string"}}},
        ["status", "reason", "segment_ids", "evidence_ids"])]


def demo_records(request, overrides=None):
    """Four small local data sources. No business API or planning engine is involved."""
    data = json.loads((PACKAGE / "data/demo.json").read_text(encoding="utf-8"))
    meeting = datetime.fromisoformat(request["meeting_at"])
    dates = {"departure_date": str(meeting.date() - timedelta(days=1)), "meeting_date": str(meeting.date())}
    for records in data.values():
        for record in records:
            for key, value in record.items():
                if isinstance(value, str):
                    record[key] = value.format(**dates)
    if (request["origin"], request["destination"], request["meeting_location"]) != ("杭州", "上海", "东方明珠塔"):
        return {key: records if key == "get_policy" else [] for key, records in data.items()}
    overrides = overrides or {}
    if "hotel_price_cny" in overrides:
        data["search_hotels"][0]["price_cny"] = overrides["hotel_price_cny"]
    if overrides.get("missing_hotel"):
        data["search_hotels"] = []
    if overrides.get("late_arrival"):
        data["search_local_transport"][-1]["end_at"] = f'{dates["meeting_date"]}T08:10+08:00'
    return data


class Tools:
    STATE_FIELDS = ("_data", "_faults", "seen", "calls")

    def __init__(self, scenario, request):
        self._data = demo_records(request, scenario.get("overrides"))
        self._faults = dict(scenario.get("faults", {}))
        self.seen = {}
        self.calls = 0

    def snapshot(self):
        return copy.deepcopy({key: getattr(self, key) for key in self.STATE_FIELDS})

    def restore(self, state):
        if set(state) != set(self.STATE_FIELDS):
            raise ValueError("Incompatible demo tool state")
        for key in self.STATE_FIELDS:
            setattr(self, key, copy.deepcopy(state[key]))

    def execute(self, name, args):
        self.calls += 1
        if name not in TOOL_NAMES or not isinstance(args, dict) or args:
            raise ValueError("Unknown tool or unexpected arguments")
        if self._faults.get(name, 0) > 0:
            self._faults[name] -= 1
            raise TimeoutError("Simulated backend timeout")
        records = copy.deepcopy(self._data[name])
        for record in records:
            record["source"] = name
            self.seen[record["id"]] = record
        return {"records": records}
