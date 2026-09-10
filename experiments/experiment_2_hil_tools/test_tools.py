from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path

from .run import _filter_transport_by_arrival_date, _with_retries
from .tools import HilTools

ROOT = Path(__file__).resolve().parent
POLICY = ROOT / "policies" / "浙江省机关工作人员差旅费管理规定.md"


class StubCapture:
    def __init__(self, tuniu_payloads=None, amap_payloads=None):
        self.tuniu_payloads = iter(tuniu_payloads or [])
        self.amap_payloads = iter(amap_payloads or [])
        self.calls = []

    async def tuniu(self, service, tool, arguments, expected=(), parent_id=None):
        self.calls.append(("tuniu", service, tool, arguments))
        record = next(self.tuniu_payloads)
        record.setdefault("operation", f"{service}.{tool}")
        return record

    async def amap(self, operation, arguments, expected=(), parent_id=None):
        self.calls.append(("amap", operation, arguments))
        record = next(self.amap_payloads)
        record.setdefault("operation", f"amap.{operation}")
        return record


def ok(call_id: str, payload: dict) -> dict:
    return {"status": "ok", "call_id": call_id, "payloads": [payload]}


def test_transport_filter_removes_next_day_arrival() -> None:
    result = {
        "status": "ok",
        "data": {
            "candidates": [
                {"flight_no": "SAME", "arrival_at": "2026-09-18 18:55"},
                {"flight_no": "NEXT", "arrival_at": "2026-09-19 01:25"},
            ]
        },
        "warnings": [],
    }

    filtered = _filter_transport_by_arrival_date(result, date(2026, 9, 18))

    assert [item["flight_no"] for item in filtered["data"]["candidates"]] == ["SAME"]
    assert filtered["data"]["filtered_out_count"] == 1


def test_retry_stops_after_success() -> None:
    calls = 0

    async def operation() -> dict:
        nonlocal calls
        calls += 1
        return {"status": "ok"} if calls == 3 else {"status": "error"}

    result, attempts = asyncio.run(_with_retries("test", operation, max_retries=3))

    assert result["status"] == "ok"
    assert attempts == 3


def test_empty_transport_service_is_not_reported_as_tool_failure() -> None:
    capture = StubCapture(
        tuniu_payloads=[
            {
                "status": "error",
                "call_id": "train-empty",
                "payloads": [{"successCode": False, "data": None}],
                "error_type": "ValueError",
            }
        ]
    )

    result = asyncio.run(
        HilTools(capture, POLICY).search_transport(
            mode="train",
            origin="杭州",
            destination="三亚",
            departure_date="2026-09-18",
            service_class="high_speed_rail",
        )
    )

    assert result["status"] == "ok"
    assert result["data"]["candidates"] == []
    assert "no transport service" in result["warnings"][0]


def test_hotel_detail_converts_numeric_id_for_supplier() -> None:
    capture = StubCapture(
        tuniu_payloads=[
            ok(
                "hotel-detail",
                {
                    "hotelId": 34548,
                    "hotelName": "Test Hotel",
                    "roomTypes": [],
                },
            )
        ]
    )

    result = asyncio.run(
        HilTools(capture, POLICY).get_stay_detail(
            hotel_id="34548", check_in="2026-09-18", check_out="2026-09-19"
        )
    )

    assert result["status"] == "ok"
    assert capture.calls[0][3]["hotelId"] == 34548


def test_route_duration_can_use_cost_duration() -> None:
    capture = StubCapture(
        amap_payloads=[
            ok(
                "route",
                {
                    "route": {
                        "paths": [
                            {"distance": "1000", "cost": {"duration": "120"}, "steps": []}
                        ]
                    }
                },
            )
        ]
    )

    result = asyncio.run(
        HilTools(capture, POLICY).calculate_route(
            origin="120,30", destination="121,31"
        )
    )

    assert result["status"] == "ok"
    assert result["data"]["paths"][0]["duration_seconds"] == 120


def test_experiment_two_does_not_import_experiment_one() -> None:
    source_files = [path for path in ROOT.glob("*.py") if path.name != "test_tools.py"]
    assert source_files
    assert all("experiments.external_tools" not in path.read_text(encoding="utf-8") for path in source_files)
