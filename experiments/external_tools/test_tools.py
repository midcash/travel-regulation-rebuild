"""离线合同测试：固定供应商样例，检查实际请求和业务数值。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from .amap import AmapRouteProvider
from .clock import SystemClock
from .config import Settings
from .errors import (
    ToolAuthenticationError,
    ToolBusinessError,
    ToolConfigurationError,
    ToolConstraintNoMatchError,
    ToolEmptyResultError,
    ToolRateLimitError,
    ToolResponseSchemaError,
    ToolTimeoutError,
    ToolTransportError,
)
from .models import RouteQuery, StayQuery, TransportQuery
from .policy import PolicyDocumentError, PolicySnapshotLoader
from .tuniu import TuniuTravelProvider

POLICY = Path(__file__).with_name("policy.md")
FLIGHT = {
    "flightNumber": "CA123",
    "airlineCompany": "<b>Air</b>",
    "departureTime": "2026-09-12 07:20",
    "arrivalTime": "2026-09-12 09:55",
    "basePrice": "710.10",
    "totalTax": "50.20",
}
TRAIN = {
    "trainNum": "G123",
    "trainType": "direct",
    "departStationName": "北京南",
    "destStationName": "上海虹桥",
    "departureTime": "2026-09-12 07:20",
    "arrivalTime": "2026-09-12 12:00",
    "price": {"edzPrice": "73", "ydzPrice": "117", "wzPrice": "0"},
}
HOTEL = {
    "hotelId": "123",
    "hotelName": "<b>示例酒店</b>",
    "cityName": "上海",
    "lowestPrice": "600",
}


def transport(**changes: object) -> TransportQuery:
    return TransportQuery(
        **(
            {
                "query_id": "test-transport",
                "trace_id": "test-trace",
                "origin": "北京",
                "destination": "上海",
                "departure_after": "2026-09-12T06:00:00+08:00",
                "arrival_before": "2026-09-12T08:00:00+08:00",
                "travelers": 2,
            }
            | changes
        )
    )


def stay(**changes: object) -> StayQuery:
    return StayQuery(
        **(
            {
                "query_id": "test-stay",
                "trace_id": "test-trace",
                "destination": "上海",
                "date_range": {"start": "2026-09-12", "end": "2026-09-14"},
                "travelers": 2,
                "area": "外滩",
                "max_total_price": {"amount": "900", "currency": "CNY"},
            }
            | changes
        )
    )


def route() -> RouteQuery:
    return RouteQuery(
        query_id="test-route",
        trace_id="test-trace",
        origin={"longitude": 121.443, "latitude": 31.197},
        destination={"longitude": 121.499, "latitude": 31.239},
    )


def call(
    query,
    payload: dict | None = None,
    *,
    status: int = 200,
    sse: bool = False,
    failure: Exception | None = None,
):
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if failure:
            raise failure
        if isinstance(query, RouteQuery):
            return httpx.Response(status, json=payload)
        envelope = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": [{"type": "text", "text": json.dumps(payload)}]},
        }
        if sse:
            return httpx.Response(
                status,
                content="data: " + json.dumps(envelope) + "\n\n",
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(status, json=envelope)

    async def execute():
        settings = Settings(
            amap_api_key="test-only",
            tuniu_api_key="test-only",
            tuniu_flight_url="https://example.test/flight",
            tuniu_hotel_url="https://example.test/hotel",
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            provider = (
                AmapRouteProvider
                if isinstance(query, RouteQuery)
                else TuniuTravelProvider
            )(settings, client, SystemClock())
            return await provider.search(query, timeout_seconds=query.timeout_seconds)

    result = asyncio.run(execute())
    assert len(requests) == 1
    return result, requests[0]


@pytest.mark.parametrize("sse", [False, True])
def test_flight_price_request_and_unfiltered_arrival(sse: bool) -> None:
    query = transport()
    result, request = call(query, {"successCode": True, "data": [FLIGHT]}, sse=sse)
    item = result.items[0]
    assert str(item.total_price.amount) == "760.30"
    assert item.name == "Air CA123"
    assert item.arrival_at > query.arrival_before  # 适配器确实不执行到达时间过滤。
    assert item.source_ref == result.source_ref == "https://example.test/flight"
    params = json.loads(request.content)["params"]
    assert params == {
        "name": "searchLowestPriceFlight",
        "arguments": {
            "departureCityName": "北京",
            "arrivalCityName": "上海",
            "departureDate": "2026-09-12",
        },
    }
    assert request.headers["apiKey"] == "test-only"


def test_china_date_at_utc_boundary() -> None:
    _, request = call(
        transport(departure_after="2026-09-11T18:00:00Z", arrival_before=None),
        {"successCode": True, "data": [FLIGHT]},
    )
    assert (
        json.loads(request.content)["params"]["arguments"]["departureDate"]
        == "2026-09-12"
    )


def test_train_price_and_high_speed_filter() -> None:
    query = transport(mode="train", service_class="high_speed_rail")
    result, request = call(
        query, {"successCode": True, "data": [TRAIN, TRAIN | {"trainNum": "K1"}]}
    )
    assert len(result.items) == 1
    assert result.items[0].total_price.amount == 73
    assert result.items[0].departure_station == "北京南"
    assert json.loads(request.content)["params"]["name"] == "searchLowestPriceTrain"
    with pytest.raises(ToolConstraintNoMatchError):
        call(query, {"successCode": True, "data": [TRAIN | {"trainNum": "K1"}]})


def test_hotel_quote_is_not_multiplied_by_nights_or_people() -> None:
    result, request = call(stay(), {"success": True, "hotels": [HOTEL]})
    item = result.items[0]
    assert item.total_price.amount == 600
    assert item.location is None and item.refundable is None
    assert item.name == "示例酒店" and item.area == "上海"
    assert item.source_ref == result.source_ref == "https://example.test/hotel"
    assert (item.check_out - item.check_in).days == 2
    assert json.loads(request.content)["params"] == {
        "name": "tuniuHotelSearch",
        "arguments": {
            "cityName": "上海",
            "checkIn": "2026-09-12",
            "checkOut": "2026-09-14",
            "adultNum": 2,
            "keyword": "外滩",
            "prices": "0-900",
        },
    }


def test_route_units_and_gcj02_request() -> None:
    result, request = call(
        route(),
        {"status": "1", "route": {"paths": [{"duration": "600", "distance": "5000"}]}},
    )
    assert result.items[0].duration_seconds == 600
    assert result.items[0].distance_meters == 5000
    assert request.url.params["origin"] == "121.443000,31.197000"
    assert request.url.params["strategy"] == "32"
    assert route().origin.crs == "GCJ-02"


@pytest.mark.parametrize(
    "query,payload,error",
    [
        (transport(), {"successCode": True, "data": []}, ToolEmptyResultError),
        (stay(), {"success": True, "hotels": []}, ToolEmptyResultError),
        (route(), {"status": "1", "route": {"paths": []}}, ToolEmptyResultError),
        (route(), {"status": "1", "route": {"paths": {}}}, ToolResponseSchemaError),
        (
            route(),
            {"status": "1", "route": {"paths": [{"duration": "1", "distance": "bad"}]}},
            ToolResponseSchemaError,
        ),
        (
            transport(),
            {"successCode": True, "data": [FLIGHT | {"basePrice": 1.25}]},
            ToolResponseSchemaError,
        ),
        (stay(), {"success": False}, ToolBusinessError),
        (route(), {"status": "0", "infocode": "10001"}, ToolAuthenticationError),
    ],
)
def test_empty_and_malformed_responses_fail(
    query, payload: dict, error: type[Exception]
) -> None:
    with pytest.raises(error) as caught:
        call(query, payload)
    assert "test-only" not in str(caught.value)
    assert caught.value.operation == (
        "route_search"
        if isinstance(query, RouteQuery)
        else "stay_search"
        if isinstance(query, StayQuery)
        else "transport_search"
    )


@pytest.mark.parametrize("query", [transport(), route()])
@pytest.mark.parametrize(
    "status,error",
    [
        (401, ToolAuthenticationError),
        (429, ToolRateLimitError),
        (503, ToolTransportError),
        (302, ToolTransportError),
    ],
)
def test_http_failures(query, status: int, error: type[Exception]) -> None:
    with pytest.raises(error):
        call(query, {}, status=status)


@pytest.mark.parametrize("query", [transport(), route()])
def test_timeout_is_explicit(query) -> None:
    with pytest.raises(ToolTimeoutError):
        call(query, failure=httpx.ReadTimeout("sensitive transport details"))


@pytest.mark.skip(reason="政策工具按本轮要求保持原状，旧示例 policy.md 已移除")
def test_policy_and_invalid_utf8(tmp_path: Path) -> None:
    snapshot = PolicySnapshotLoader().load(POLICY)
    assert len(snapshot.rules) == 6
    assert snapshot.rule("meeting_arrival_buffer").value == 30
    assert snapshot.rule("local_transport_allowed").value is True
    assert snapshot.content_hash == hashlib.sha256(POLICY.read_bytes()).hexdigest()
    with pytest.raises(KeyError):
        snapshot.rule("missing-rule")
    with pytest.raises(PolicyDocumentError):
        PolicySnapshotLoader().load(tmp_path / "missing.md")
    bad = tmp_path / "bad.md"
    bad.write_bytes(b"\xff")
    with pytest.raises(PolicyDocumentError):
        PolicySnapshotLoader().load(bad)


def test_query_boundaries_and_missing_key() -> None:
    with pytest.raises(ValidationError):
        stay(date_range={"start": "2026-09-12", "end": "2026-09-12"})
    with pytest.raises(ValidationError):
        stay(max_total_price={"amount": "100", "currency": "USD"})
    with pytest.raises(ValidationError):
        route().origin.__class__(longitude=120, latitude=30, crs="WGS84")
    with pytest.raises(ValidationError):
        transport(departure_after="2026-09-12T06:00:00")
    with pytest.raises(ToolConfigurationError):
        TuniuTravelProvider(Settings(), httpx.AsyncClient(), SystemClock())
