"""Seven compact business tools for a human-in-the-loop travel agent.

The tools call the already verified read-only supplier adapters, but return a
small decision-facing DTO instead of exposing raw supplier payloads to the
agent.  Every response keeps evidence references to the Capture records.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from experiments.external_tools.capture import Capture, payload
from .regulation_policy import RegulationPolicyError, load_regulation_policy

POI_FIELDS = "children,business,indoor,navi,photos"

TOOL_SPECS = (
    {
        "name": "search_transport",
        "description": "搜索航班或火车候选，返回可比较的摘要。",
        "input": "mode, origin, destination, departure_date, service_class, max_results",
    },
    {
        "name": "get_transport_detail",
        "description": "获取已选交通候选的舱位/席别、价格和退改信息。",
        "input": "mode plus flight_no or train_no and station/date fields",
    },
    {
        "name": "search_stays",
        "description": "按城市、日期和会议地点搜索住宿候选。",
        "input": "destination, check_in, check_out, meeting_place, max_price, max_results",
    },
    {
        "name": "get_stay_detail",
        "description": "获取已选酒店的房型、报价、早餐和取消规则。",
        "input": "hotel_id, check_in, check_out, adults",
    },
    {
        "name": "resolve_place",
        "description": "解析会议地点、酒店、机场或车站并返回候选坐标。",
        "input": "city, keyword, max_results",
    },
    {
        "name": "calculate_route",
        "description": "计算两个 GCJ-02 坐标间的驾车路线。",
        "input": "origin, destination, optional place ids",
    },
    {
        "name": "get_policy_snapshot",
        "description": "读取带版本、有效期和哈希的政策快照。",
        "input": "no user input; uses the controlled policy file",
    },
)


class HilTools:
    """Business-facing facade over Tuniu, Amap and the local policy file."""

    def __init__(self, capture: Capture, policy_path: Path) -> None:
        self.capture = capture
        self.policy_path = policy_path

    async def search_transport(
        self,
        *,
        mode: str,
        origin: str,
        destination: str,
        departure_date: date | str,
        service_class: str = "any",
        max_results: int = 5,
    ) -> dict[str, Any]:
        """Return short candidate summaries for one transport mode."""
        if mode not in {"flight", "train"}:
            return _error("search_transport", "InputError", "mode must be flight or train")
        if max_results < 1:
            return _error("search_transport", "InputError", "max_results must be positive")
        tool = "searchLowestPriceFlight" if mode == "flight" else "searchLowestPriceTrain"
        record = await self.capture.tuniu(
            mode,
            tool,
            {
                "departureCityName": origin,
                "arrivalCityName": destination,
                "departureDate": str(departure_date),
            },
            expected=("data",),
        )
        data, failure = _payload_or_error(record)
        if failure:
            return _error("search_transport", **failure)
        rows = data.get("data", [])
        if not isinstance(rows, list):
            return _error("search_transport", "SchemaError", "supplier data is not a list", record)
        if mode == "train" and service_class == "high_speed_rail":
            rows = [row for row in rows if _is_high_speed(row)]
        candidates = [
            _flight_summary(row, record["call_id"])
            if mode == "flight"
            else _train_summary(row, record["call_id"])
            for row in rows[:max_results]
            if isinstance(row, Mapping)
        ]
        return _ok(
            "search_transport",
            {
                "mode": mode,
                "origin": origin,
                "destination": destination,
                "departure_date": str(departure_date),
                "service_class": service_class,
                "candidates": candidates,
                "supplier_total_pages": data.get("totalPageNum"),
                "returned_count": len(candidates),
            },
            [record["call_id"]],
            ["service_class filtering is applied locally for high_speed_rail"]
            if mode == "train" and service_class == "high_speed_rail"
            else [],
        )

    async def get_transport_detail(
        self,
        *,
        mode: str,
        departure_date: date | str,
        origin: str | None = None,
        destination: str | None = None,
        flight_no: str | None = None,
        train_no: str | None = None,
        departure_station: str | None = None,
        arrival_station: str | None = None,
        max_options: int = 20,
    ) -> dict[str, Any]:
        """Return detail only after a candidate has been selected."""
        if mode == "flight":
            if not all((origin, destination, flight_no)):
                return _error("get_transport_detail", "InputError", "flight detail requires origin, destination and flight_no")
            service = "flight"
            tool = "multiCabinDetails"
            args = {
                "departureCityName": origin,
                "arrivalCityName": destination,
                "departureDate": str(departure_date),
                "flightNo": flight_no,
            }
        elif mode == "train":
            if not all((departure_station, arrival_station, train_no)):
                return _error("get_transport_detail", "InputError", "train detail requires station names and train_no")
            service = "train"
            tool = "queryTrainDetail"
            args = {
                "departureStationName": departure_station,
                "arrivalStationName": arrival_station,
                "departureDate": str(departure_date),
                "trainNum": train_no,
            }
        else:
            return _error("get_transport_detail", "InputError", "mode must be flight or train")

        record = await self.capture.tuniu(service, tool, args, expected=("data",) if mode == "train" else ("cabinInfo",))
        data, failure = _payload_or_error(record)
        if failure:
            return _error("get_transport_detail", **failure)
        if mode == "flight":
            return _ok(
                "get_transport_detail",
                {
                    "mode": mode,
                    "flight": _first_mapping(data.get("flightInfo")),
                    "cabin_options": [
                        _flight_cabin(row)
                        for row in data.get("cabinInfo", [])[:max_options]
                        if isinstance(row, Mapping)
                    ],
                },
                [record["call_id"]],
            )
        detail = data.get("data")
        if not isinstance(detail, Mapping):
            return _error("get_transport_detail", "SchemaError", "train detail data is not an object", record)
        seats = detail.get("seatInfo", [])
        return _ok(
            "get_transport_detail",
            {
                "mode": mode,
                "train": detail.get("trainInfo", detail),
                "seat_options": [
                    _train_seat(row)
                    for row in seats[:max_options]
                    if isinstance(row, Mapping)
                ],
            },
            [record["call_id"]],
        )

    async def search_stays(
        self,
        *,
        destination: str,
        check_in: date | str,
        check_out: date | str,
        meeting_place: str | None = None,
        max_price: str | int | Decimal | None = None,
        max_results: int = 5,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {
            "cityName": destination,
            "checkIn": str(check_in),
            "checkOut": str(check_out),
            "adultNum": 1,
        }
        if meeting_place:
            args["poiName"] = meeting_place
        if max_price is not None:
            args["prices"] = f"0-{max_price}"
        record = await self.capture.tuniu(
            "hotel", "tuniuHotelSearch", args, expected=("hotels",)
        )
        data, failure = _payload_or_error(record)
        if failure:
            return _error("search_stays", **failure)
        rows = data.get("hotels", [])
        if not isinstance(rows, list):
            return _error("search_stays", "SchemaError", "supplier hotels is not a list", record)
        candidates = [
            _hotel_summary(row, record["call_id"])
            for row in rows[:max_results]
            if isinstance(row, Mapping)
        ]
        return _ok(
            "search_stays",
            {
                "destination": destination,
                "check_in": str(check_in),
                "check_out": str(check_out),
                "meeting_place": meeting_place,
                "candidates": candidates,
                "returned_count": len(candidates),
            },
            [record["call_id"]],
        )

    async def get_stay_detail(
        self,
        *,
        hotel_id: str,
        check_in: date | str,
        check_out: date | str,
        adults: int = 1,
        max_rooms: int = 10,
        max_rates_per_room: int = 5,
    ) -> dict[str, Any]:
        record = await self.capture.tuniu(
            "hotel",
            "tuniuHotelDetail",
            {
                "hotelId": hotel_id,
                "checkIn": str(check_in),
                "checkOut": str(check_out),
                "roomNum": 1,
                "adultNum": adults,
            },
            expected=("roomTypes",),
        )
        data, failure = _payload_or_error(record)
        if failure:
            return _error("get_stay_detail", **failure)
        rooms = data.get("roomTypes", [])
        return _ok(
            "get_stay_detail",
            {
                "hotel": {
                    "hotel_id": data.get("hotelId", hotel_id),
                    "hotel_name": data.get("hotelName"),
                    "star_name": data.get("starName"),
                    "comment_score": data.get("commentScore"),
                    "policies": data.get("policies"),
                    "reviews": data.get("reviews"),
                },
                "room_types": [
                    _room_summary(room, max_rates_per_room)
                    for room in rooms[:max_rooms]
                    if isinstance(room, Mapping)
                ],
            },
            [record["call_id"]],
        )

    async def resolve_place(
        self, *, city: str, keyword: str, max_results: int = 5
    ) -> dict[str, Any]:
        search = await self.capture.amap(
            "poi_search",
            {
                "keywords": keyword,
                "region": city,
                "city_limit": "true",
                "show_fields": POI_FIELDS,
                "page_size": max_results,
                "page_num": 1,
            },
            expected=("pois",),
        )
        data, failure = _payload_or_error(search)
        if failure:
            return _error("resolve_place", **failure)
        rows = data.get("pois", [])
        if not isinstance(rows, list):
            return _error("resolve_place", "SchemaError", "supplier pois is not a list", search)
        evidence = [search["call_id"]]
        candidates = []
        for index, row in enumerate(rows[:max_results], start=1):
            if not isinstance(row, Mapping):
                continue
            item = _place_summary(row, index, search["call_id"])
            if index == 1 and row.get("id"):
                detail = await self.capture.amap(
                    "poi_detail", {"id": row["id"], "show_fields": POI_FIELDS}, search["call_id"]
                )
                if detail["status"] == "ok":
                    evidence.append(detail["call_id"])
                    detail_data, _ = _payload_or_error(detail)
                    item["detail"] = _first_mapping(detail_data.get("pois"))
                geocode = await self.capture.amap(
                    "geocode", {"address": f"{city}{row.get('address') or ''}{row.get('name') or ''}", "city": city}, search["call_id"]
                )
                if geocode["status"] == "ok":
                    evidence.append(geocode["call_id"])
                    geocode_data, _ = _payload_or_error(geocode)
                    item["geocode"] = _first_mapping(geocode_data.get("geocodes"))
            candidates.append(item)
        return _ok(
            "resolve_place",
            {
                "city": city,
                "keyword": keyword,
                "candidates": candidates,
                "identity_confirmed": False,
                "confidence_basis": "supplier rank plus exact-name/address fields; human confirmation remains possible",
            },
            evidence,
        )

    async def calculate_route(
        self,
        *,
        origin: str,
        destination: str,
        origin_id: str | None = None,
        destination_id: str | None = None,
        max_paths: int = 3,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {
            "origin": origin,
            "destination": destination,
            "strategy": 32,
            "show_fields": "cost,tmcs,navi,cities,polyline",
        }
        if origin_id:
            args["origin_id"] = origin_id
        if destination_id:
            args["destination_id"] = destination_id
        record = await self.capture.amap("driving_v5", args, expected=("route",))
        evidence = [record["call_id"]]
        warnings: list[str] = []
        if record["status"] != "ok":
            warnings.append("driving_v5 failed; fell back to driving_v3")
            record = await self.capture.amap(
                "driving_v3",
                {"origin": origin, "destination": destination, "strategy": 32, "extensions": "base"},
                expected=("route",),
                parent_id=evidence[0],
            )
            evidence.append(record["call_id"])
        data, failure = _payload_or_error(record)
        if failure:
            return _error("calculate_route", **failure, evidence=evidence, warnings=warnings)
        route = data.get("route")
        paths = route.get("paths", []) if isinstance(route, Mapping) else []
        return _ok(
            "calculate_route",
            {
                "origin": origin,
                "destination": destination,
                "provider_operation": record["operation"],
                "paths": [_route_summary(path, index) for index, path in enumerate(paths[:max_paths], 1) if isinstance(path, Mapping)],
            },
            evidence,
            warnings,
        )

    def get_policy_snapshot(self) -> dict[str, Any]:
        try:
            snapshot = load_regulation_policy(self.policy_path)
        except (RegulationPolicyError, OSError, ValueError) as exc:
            return _error("get_policy_snapshot", type(exc).__name__, str(exc))
        return _ok(
            "get_policy_snapshot",
            snapshot,
            [f"policy:{snapshot['content_hash']}"],
        )


def _ok(tool: str, data: Any, evidence: list[str], warnings: list[str] | None = None) -> dict[str, Any]:
    return {"tool": tool, "status": "ok", "data": data, "evidence_refs": evidence, "warnings": warnings or []}


def _error(tool: str, error_type: str, message: str, record: Mapping[str, Any] | None = None, *, evidence: list[str] | None = None, warnings: list[str] | None = None) -> dict[str, Any]:
    refs = list(evidence or [])
    if record and record.get("call_id"):
        refs.append(str(record["call_id"]))
    return {"tool": tool, "status": "error", "data": None, "error": {"type": error_type, "message": message}, "evidence_refs": refs, "warnings": warnings or []}


def _payload_or_error(record: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if record.get("status") != "ok":
        return {}, {"error_type": str(record.get("error_type", "SupplierError")), "message": str(record.get("error", "supplier call failed")), "record": record}
    value = payload(record)
    if not isinstance(value, dict):
        return {}, {"error_type": "SchemaError", "message": "supplier payload is not an object", "record": record}
    return value, None


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _money(value: Any) -> str | None:
    parsed = _decimal(value)
    return format(parsed, "f") if parsed is not None else None


def _first_mapping(value: Any) -> dict[str, Any] | None:
    if isinstance(value, list) and value and isinstance(value[0], Mapping):
        return dict(value[0])
    if isinstance(value, Mapping):
        return dict(value)
    return None


def _is_high_speed(row: Mapping[str, Any]) -> bool:
    train_no = str(row.get("trainNum", "")).upper()
    return train_no.startswith(("G", "D", "C"))


def _flight_summary(row: Mapping[str, Any], evidence: str) -> dict[str, Any]:
    base = _decimal(row.get("basePrice"))
    tax = _decimal(row.get("totalTax"))
    total = base + tax if base is not None and tax is not None else _decimal(row.get("totalPrice"))
    return {
        "candidate_id": f"flight:{row.get('flightNumber', 'unknown')}",
        "mode": "flight",
        "flight_no": _text(row.get("flightNumber")),
        "operator": _text(row.get("airlineCompany")),
        "departure_at": _text(row.get("departureTime")),
        "arrival_at": _text(row.get("arrivalTime")),
        "departure_airport": _text(row.get("departureAirport")),
        "arrival_airport": _text(row.get("arrivalAirport")),
        "price_cny": format(total, "f") if total is not None else None,
        "base_price_cny": _money(row.get("basePrice")),
        "tax_cny": _money(row.get("totalTax")),
        "remaining_seats": row.get("remainingSeats"),
        "evidence_ref": evidence,
    }


def _train_summary(row: Mapping[str, Any], evidence: str) -> dict[str, Any]:
    prices = row.get("price") if isinstance(row.get("price"), Mapping) else {}
    numeric = [_decimal(value) for value in prices.values() if _decimal(value) is not None and _decimal(value) > 0]
    lowest = min(numeric) if numeric else None
    return {
        "candidate_id": f"train:{row.get('trainNum', 'unknown')}",
        "mode": "train",
        "train_no": _text(row.get("trainNum")),
        "train_type": _text(row.get("trainType")),
        "departure_station": _text(row.get("departStationName")),
        "arrival_station": _text(row.get("destStationName")),
        "departure_at": _text(row.get("departureTime")),
        "arrival_at": _text(row.get("arrivalTime")),
        "duration": _text(row.get("duration")),
        "lowest_price_cny": format(lowest, "f") if lowest is not None else None,
        "seat_prices": dict(prices),
        "evidence_ref": evidence,
    }


def _flight_cabin(row: Mapping[str, Any]) -> dict[str, Any]:
    return {key: row.get(key) for key in ("cabinPriceId", "basePrice", "totalTax", "cabinClass", "cabinClassCodes", "buyCondition", "refundChangeRule", "baggageInfo", "mealCode", "reimbursementProof", "remainingSeats") if key in row}


def _train_seat(row: Mapping[str, Any]) -> dict[str, Any]:
    return {key: row.get(key) for key in ("leftNumber", "seatId", "seatName", "price", "resId", "seatStatus") if key in row}


def _hotel_summary(row: Mapping[str, Any], evidence: str) -> dict[str, Any]:
    return {
        "candidate_id": f"hotel:{row.get('hotelId', 'unknown')}",
        "hotel_id": _text(row.get("hotelId")),
        "name": _text(row.get("hotelName")),
        "star_name": _text(row.get("starName")),
        "comment_score": row.get("commentScore"),
        "address": _text(row.get("address")),
        "brand": _text(row.get("brandName")),
        "lowest_price_raw": row.get("lowestPrice"),
        "distance": row.get("distance"),
        "refund": row.get("refund"),
        "room_name": _text(row.get("roomName")),
        "evidence_ref": evidence,
    }


def _room_summary(row: Mapping[str, Any], max_rates: int) -> dict[str, Any]:
    rates = row.get("ratePlans", [])
    return {
        "room_type_id": row.get("roomTypeId"),
        "room_type_name": row.get("roomTypeName"),
        "bed_type": row.get("bedType"),
        "max_occupancy": row.get("maxOccupancy"),
        "room_size": row.get("roomSize"),
        "floor": row.get("floor"),
        "rate_plans": [
            {key: rate.get(key) for key in ("ratePlanName", "vendorRatePlanId", "rmbPrices", "mealText", "cancelDesc", "count") if key in rate}
            for rate in rates[:max_rates]
            if isinstance(rate, Mapping)
        ],
    }


def _place_summary(row: Mapping[str, Any], rank: int, evidence: str) -> dict[str, Any]:
    name = _text(row.get("name")) or ""
    return {
        "place_id": _text(row.get("id")),
        "rank": rank,
        "name": name,
        "address": _text(row.get("address")),
        "location": _text(row.get("location")),
        "city": _text(row.get("cityname")),
        "district": _text(row.get("adname")),
        "match_confidence": "supplier_ranked",
        "evidence_ref": evidence,
    }


def _route_summary(path: Mapping[str, Any], rank: int) -> dict[str, Any]:
    return {
        "route_id": f"route:{rank}",
        "rank": rank,
        "distance_meters": _int_or_none(path.get("distance")),
        "duration_seconds": _int_or_none(path.get("duration")),
        "restriction": path.get("restriction"),
        "cost": path.get("cost"),
        "steps_count": len(path.get("steps", [])) if isinstance(path.get("steps"), list) else None,
        "has_polyline": any("polyline" in step for step in path.get("steps", []) if isinstance(step, Mapping)),
    }


def _int_or_none(value: Any) -> int | None:
    try:
        return int(str(value)) if value is not None else None
    except (TypeError, ValueError):
        return None
