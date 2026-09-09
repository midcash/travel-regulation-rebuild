"""PyCharm 直接运行；全字段查询实验，不涉及政策或主链路。"""

from __future__ import annotations

import argparse
import asyncio
import math
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from experiments.external_tools.capture import Capture, payload
from experiments.external_tools.config import load_settings
from experiments.external_tools.field_catalog import CATALOG, SOURCES

HERE = Path(__file__).resolve().parent
CHINA = timezone(timedelta(hours=8))
POI_FIELDS = "children,business,indoor,navi,photos"


async def tuniu(capture, service, tool, args, parent=None):
    return await capture.tuniu(
        service, tool, args, CATALOG.get(f"{service}.{tool}", []), parent
    )


async def amap(capture, operation, args, parent=None):
    return await capture.amap(
        operation, args, CATALOG.get(f"amap.{operation}", []), parent
    )


async def search_pages(capture, service, tool, arguments, limit):
    first = await tuniu(capture, service, tool, arguments)
    records = [first]
    data = payload(first)
    pages = int(data.get("totalPageNum") or 1)
    for page in range(2, min(pages, limit) + 1):
        args = (
            {**arguments, "pageNum": page}
            if service == "flight"
            else {"queryId": data.get("queryId"), "pageNum": page}
        )
        if service != "flight" and not args["queryId"]:
            break
        record = await tuniu(capture, service, tool, args, first["call_id"])
        records.append(record)
        if record["status"] != "ok":
            break
    capture.write(
        f"{service}_pagination.json",
        {
            "first_call_id": first["call_id"],
            "reported_total_pages": pages,
            "successful_pages": sum(r["status"] == "ok" for r in records),
            "all_reported_pages_received": first["status"] == "ok"
            and len(records) == pages
            and all(r["status"] == "ok" for r in records),
            "configured_page_limit": limit,
        },
    )
    return records


async def travel(capture, args, day):
    common = {
        "departureCityName": args.origin,
        "arrivalCityName": args.destination,
        "departureDate": day.isoformat(),
    }
    selected = {}
    for service, search, detail, list_key in (
        ("flight", "searchLowestPriceFlight", "multiCabinDetails", "data"),
        ("train", "searchLowestPriceTrain", "queryTrainDetail", "data"),
        ("hotel", "tuniuHotelSearch", "tuniuHotelDetail", "hotels"),
    ):
        if args.only not in {"all", service}:
            continue
        await tuniu(capture, service, "tools/list", {})
        search_args = dict(common)
        if service == "hotel":
            search_args = {
                "cityName": args.destination,
                "checkIn": day.isoformat(),
                "checkOut": (day + timedelta(days=args.nights)).isoformat(),
                "adultNum": 1,
                "poiName": args.meeting,
            }
        pages = await search_pages(
            capture, service, search, search_args, args.max_pages
        )
        items = [
            (item, record)
            for record in pages
            for item in payload(record).get(list_key, [])
        ]
        selected[service] = items[0] if items else None
        # 每条实际响应完整保存；详情抽样量与字段保留范围是两件事。
        for item, parent in items[: args.details]:
            if service == "flight":
                detail_args = {**common, "flightNo": item["flightNumber"]}
            elif service == "train":
                detail_args = {
                    "departureStationName": item["departStationName"],
                    "arrivalStationName": item["destStationName"],
                    "departureDate": str(item["departureTime"])[:10],
                    "trainNum": item["trainNum"],
                }
            else:
                detail_args = {
                    "hotelId": item["hotelId"],
                    "checkIn": search_args["checkIn"],
                    "checkOut": search_args["checkOut"],
                    "roomNum": 1,
                    "adultNum": 1,
                }
            await tuniu(capture, service, detail, detail_args, parent["call_id"])
        if service == "flight":
            await tuniu(capture, service, "getBookingRequiredInfo", {})
            for mode in ("TIME", "PRICE", "NEAR_GO", "NEAR_BACK", "TRANSFER"):
                extra = (
                    {"departureTime": "06:00-12:00"}
                    if mode == "TIME"
                    else {"priceRange": "300-1500"}
                    if mode == "PRICE"
                    else {}
                )
                await tuniu(
                    capture, service, search, {**common, "searchType": mode, **extra}
                )
    return selected


async def places_and_routes(capture, args, selected):
    queries = [("meeting", args.destination, args.meeting, None)]
    if selected.get("hotel"):
        item, parent = selected["hotel"]
        queries.append(
            ("hotel", args.destination, item["hotelName"], parent["call_id"])
        )
    else:
        queries.append(("hotel_example", args.destination, args.hotel, None))
    if selected.get("train"):
        item, parent = selected["train"]
        queries.extend(
            [
                (
                    "departure_station",
                    args.origin,
                    item["departStationName"] + "站",
                    parent["call_id"],
                ),
                (
                    "arrival_station",
                    args.destination,
                    item["destStationName"] + "站",
                    parent["call_id"],
                ),
            ]
        )
    if selected.get("flight"):
        item, parent = selected["flight"]
        for side, city in (("departure", args.origin), ("arrival", args.destination)):
            airport = item.get(side + "Airport")
            if airport:
                name = airport + "机场 " + (item.get(side + "Terminal") or "")
                queries.append(
                    (side + "_airport", city, name.strip(), parent["call_id"])
                )
    locations = []
    for label, city, keyword, parent_id in queries:
        record = await amap(
            capture,
            "poi_search",
            {
                "keywords": keyword,
                "region": city,
                "city_limit": "true",
                "show_fields": POI_FIELDS,
                "page_size": 25,
                "page_num": 1,
            },
            parent_id,
        )
        pois = payload(record).get("pois", [])
        locations.append(
            {
                "role": label,
                "keyword": keyword,
                "city": city,
                "call_id": record["call_id"],
                "candidates": pois,
            }
        )
        if pois:
            first = pois[0]
            await amap(
                capture,
                "poi_detail",
                {"id": first["id"], "show_fields": POI_FIELDS},
                record["call_id"],
            )
            await amap(
                capture,
                "geocode",
                {
                    "address": city + str(first.get("address") or "") + first["name"],
                    "city": city,
                },
                record["call_id"],
            )
    capture.write(
        "place_candidates.json",
        {
            "note": "全部候选保留；供应商排序第一项仅用于接口衔接实验，不代表同名地点已确认匹配。",
            "locations": locations,
        },
    )
    endpoints = [
        entry
        for entry in locations
        if entry["candidates"] and entry["city"] == args.destination
    ]
    if len(endpoints) < 2:
        capture.write(
            "route_chain.json",
            {
                "status": "blocked",
                "reason": "Less than two POI results with destination-city context",
            },
        )
        return
    target_entry, origin_entry = endpoints[:2]
    origin, target = origin_entry["candidates"][0], target_entry["candidates"][0]
    route_args = {
        "origin": origin["location"],
        "destination": target["location"],
        "strategy": 32,
    }
    capture.write(
        "route_chain.json",
        {
            "status": "sample_candidates_selected",
            "coordinate_system": "GCJ-02",
            "origin": origin,
            "destination": target,
            "source_call_ids": [origin_entry["call_id"], target_entry["call_id"]],
            "selection": "first_supplier_candidate_for_API_experiment_only",
            "identity_confirmed": False,
        },
    )
    await amap(
        capture,
        "reverse_geocode",
        {
            "location": origin["location"],
            "extensions": "all",
            "radius": 1000,
            "roadlevel": 0,
        },
        origin_entry["call_id"],
    )
    await amap(
        capture,
        "poi_around",
        {
            "location": target["location"],
            "radius": 1000,
            "show_fields": POI_FIELDS,
            "page_size": 25,
            "page_num": 1,
        },
        target_entry["call_id"],
    )
    await amap(
        capture,
        "driving_v3",
        {**route_args, "extensions": "all"},
        origin_entry["call_id"],
    )
    await amap(
        capture,
        "driving_v5",
        {
            **route_args,
            "origin_id": origin["id"],
            "destination_id": target["id"],
            "show_fields": "cost,tmcs,navi,cities,polyline",
        },
        origin_entry["call_id"],
    )
    # 再用机场/车站候选测试较长路段，增加收费道路等条件字段的观测机会。
    for entry in endpoints[2:]:
        station = entry["candidates"][0]
        await amap(
            capture,
            "driving_v5",
            {
                "origin": station["location"],
                "destination": origin["location"],
                "origin_id": station["id"],
                "destination_id": origin["id"],
                "strategy": 32,
                "show_fields": "cost,tmcs,navi,cities,polyline",
            },
            entry["call_id"],
        )


def summarize(capture, args):
    coverage = {}
    for operation, expected in CATALOG.items():
        records = [r for r in capture.records if r["operation"] == operation]
        good = [r for r in records if r["status"] == "ok"]
        fields = {}
        for path in expected:
            states = [r.get("documented_fields", {}).get(path) for r in good]
            fields[path] = (
                "observed"
                if "observed" in states
                else "empty_or_null_only"
                if "empty_or_null_only" in states
                else "not_returned_in_samples"
                if good
                else "not_verified"
            )
        extra = sorted(
            {
                path
                for r in good
                for path in r.get("field_inventory", {})
                if path not in expected and path != "$"
            }
        )
        coverage[operation] = {
            "calls": len(records),
            "successful_calls": len(good),
            "fields": fields,
            "additional_observed_paths": extra,
        }
    capture.write("coverage.json", {"sources": SOURCES, "operations": coverage})
    all_ok = all(r["status"] == "ok" for r in capture.records) and bool(capture.records)
    summary = {
        "all_calls_succeeded": all_ok,
        "policy": "untouched_not_run",
        "sample_arguments": vars(args),
        "sources": SOURCES,
        "calls": [
            {
                k: r[k]
                for k in (
                    "call_id",
                    "parent_call_id",
                    "operation",
                    "status",
                    "file",
                    "elapsed_seconds",
                )
            }
            for r in capture.records
        ],
        "limits": [
            "All fields and records in each received response are retained; pagination and detail sample limits are explicit.",
            "A successful request does not prove every documented optional field can be returned.",
            "Only search/detail/read-only APIs are exercised; no booking, payment, cancellation or policy calls.",
            "POI candidates used for route samples are not verified cross-provider identity matches.",
        ],
    }
    summary["sample_arguments"] = {
        key: str(value) if isinstance(value, (date, Path)) else value
        for key, value in vars(args).items()
    }
    capture.write("summary.json", summary)
    return all_ok


async def run(args):
    day = args.date or datetime.now(CHINA).date() + timedelta(days=3)
    folder = (
        HERE / "results" / ("full_" + datetime.now(CHINA).strftime("%Y%m%dT%H%M%S%f"))
    )
    async with httpx.AsyncClient(
        timeout=args.timeout, follow_redirects=False
    ) as client:
        capture = Capture(load_settings(), client, folder, args.timeout)
        capture.write("field_catalog.json", {"sources": SOURCES, "fields": CATALOG})
        try:
            selected = await travel(capture, args, day)
            if args.only in {"all", "amap"}:
                await places_and_routes(capture, args, selected)
        finally:
            ok = summarize(capture, args)
            print(str(folder / "summary.json"), flush=True)
    return ok


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--only", choices=("all", "flight", "train", "hotel", "amap"), default="all"
    )
    parser.add_argument("--date", type=date.fromisoformat)
    parser.add_argument("--origin", default="北京")
    parser.add_argument("--destination", default="上海")
    parser.add_argument("--meeting", default="上海国际会议中心")
    parser.add_argument("--hotel", default="全季酒店 上海外滩金陵东路店")
    parser.add_argument("--nights", type=int, default=2)
    parser.add_argument("--max-pages", type=int, default=2)
    parser.add_argument("--details", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=45)
    args = parser.parse_args()
    if min(
        args.nights, args.max_pages, args.details, args.timeout
    ) <= 0 or not math.isfinite(args.timeout):
        parser.error("nights, max-pages, details and timeout must be positive")
    return 0 if asyncio.run(run(args)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
