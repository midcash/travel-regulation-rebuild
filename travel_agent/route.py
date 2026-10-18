"""Evidence-bound AMap driving routes between observed place candidates."""
import asyncio
from datetime import datetime
from decimal import Decimal, InvalidOperation
import math
import os
import time

import httpx

from .facts import CHINA
from .supplier_wire import dumps, loads, redact
from .transport import MAX_BYTES


def _candidate(history, candidate_id):
    matches = []
    for result in history.values():
        if result.get("tool") != "resolve_place" or result.get("status") != "ok":
            continue
        data = result.get("data") or {}
        for candidate in data.get("candidates", []):
            if isinstance(candidate, dict) and candidate.get("candidate_id") == candidate_id:
                matches.append(candidate)
    if len(matches) != 1:
        raise ValueError("Unobserved or ambiguous route endpoint")
    return matches[0]


def _candidate_at(history, location, place_id=None):
    matches = []
    for result in history.values():
        if result.get("tool") != "resolve_place" or result.get("status") != "ok":
            continue
        for candidate in (result.get("data") or {}).get("candidates", []):
            if (isinstance(candidate, dict) and candidate.get("location") == location
                    and (place_id is None or str(candidate.get("place_id")) == str(place_id))):
                matches.append(candidate)
    if len(matches) != 1:
        raise ValueError("Route coordinates must match one observed place candidate")
    return matches[0]


def _point(candidate):
    location = candidate.get("location")
    if candidate.get("coordinate_system") != "GCJ-02" or not isinstance(location, str):
        raise ValueError("Route endpoint has no sourced GCJ-02 coordinate")
    values = location.split(",")
    if len(values) != 2:
        raise ValueError("Invalid route endpoint coordinate")
    try:
        longitude, latitude = float(values[0]), float(values[1])
    except (TypeError, ValueError):
        raise ValueError("Invalid route endpoint coordinate") from None
    if not all(math.isfinite(value) for value in (longitude, latitude)):
        raise ValueError("Invalid route endpoint coordinate")
    if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
        raise ValueError("Invalid route endpoint coordinate")
    return location


def _endpoint(candidate):
    return {key: candidate.get(key) for key in (
        "candidate_id", "role", "place_id", "name", "address", "location",
        "coordinate_system", "source_candidate_id", "source_evidence_refs",
        "evidence_ref", "identity_confirmed", "match_confidence")}


async def calculate_route(args, context, call_id, folder, timeout, transport_http=None):
    if not isinstance(args, dict) or set(args) - {
        "origin_candidate_id", "destination_candidate_id", "origin", "destination",
        "origin_id", "destination_id", "max_paths"}:
        raise ValueError("Expected observed route endpoint candidates")
    max_paths = args.get("max_paths", 3)
    if type(max_paths) is not int or not 1 <= max_paths <= 10:
        raise ValueError("max_paths must be 1..10")
    _, history = context
    if isinstance(args.get("origin_candidate_id"), str) and isinstance(args.get("destination_candidate_id"), str):
        origin = _candidate(history, args["origin_candidate_id"])
        destination = _candidate(history, args["destination_candidate_id"])
    elif isinstance(args.get("origin"), str) and isinstance(args.get("destination"), str):
        # Compatibility with the experiment-two coordinate shape is safe only
        # when both coordinates resolve to exactly one observed place.
        origin = _candidate_at(history, args["origin"], args.get("origin_id"))
        destination = _candidate_at(history, args["destination"], args.get("destination_id"))
    else:
        raise ValueError("Expected observed route endpoint candidates")
    allowed = {("arrival", "hotel"), ("hotel", "meeting")}
    if (origin.get("role"), destination.get("role")) not in allowed:
        raise ValueError("Route must connect arrival to hotel or hotel to meeting")
    if origin["candidate_id"] == destination["candidate_id"]:
        raise ValueError("Route endpoints must differ")
    # Once one leg establishes the hotel, the other leg must use that same
    # observed hotel candidate. This keeps independently queried legs joined
    # without relying on a supplier route number.
    known_hotels = set()
    for previous in history.values():
        if previous.get("tool") != "calculate_route" or previous.get("status") != "ok":
            continue
        data = previous.get("data") or {}
        for key in ("origin", "destination"):
            endpoint = data.get(key) or {}
            if endpoint.get("role") == "hotel" and isinstance(endpoint.get("candidate_id"), str):
                known_hotels.add(endpoint["candidate_id"])
    current_hotels = {candidate["candidate_id"] for candidate in (origin, destination)
                      if candidate.get("role") == "hotel"}
    if known_hotels and current_hotels and not current_hotels.issubset(known_hotels):
        raise ValueError("Both route legs must use the same hotel candidate")
    origin_point, destination_point = _point(origin), _point(destination)
    query = {"origin_candidate_id": origin["candidate_id"],
             "destination_candidate_id": destination["candidate_id"],
             "origin": origin_point, "destination": destination_point,
             "origin_role": origin.get("role"), "destination_role": destination.get("role"),
             "coordinate_system": "GCJ-02"}
    deadline = time.perf_counter() + min(45, timeout)
    primary_args = {"origin": origin_point, "destination": destination_point,
                    "strategy": "32", "show_fields": "cost,tmcs,navi,cities,polyline"}
    if origin.get("place_id"):
        primary_args["origin_id"] = origin["place_id"]
    if destination.get("place_id"):
        primary_args["destination_id"] = destination["place_id"]
    primary, record = await _query("driving_v5", "/v5/direction/driving", primary_args,
                                   query, call_id, 1, folder, deadline, transport_http,
                                   origin, destination, max_paths)
    records = [record]
    if primary["status"] != "ok":
        primary["warnings"].append("driving_v5 failed; retrying with the documented driving_v3 endpoint")
        fallback_args = {"origin": origin_point, "destination": destination_point,
                         "strategy": "32", "extensions": "base"}
        fallback, fallback_record = await _query("driving_v3", "/v3/direction/driving", fallback_args,
                                                   query, call_id, 2, folder, deadline, transport_http,
                                                   origin, destination, max_paths)
        records.append(fallback_record)
        fallback["http_requests"] += primary["http_requests"]
        fallback["evidence_refs"] = primary["evidence_refs"] + fallback["evidence_refs"]
        fallback["warnings"] = primary["warnings"] + fallback["warnings"]
        primary = fallback
    return primary, {"requests": records}


async def _query(operation, path, arguments, query, call_id, index, folder, deadline,
                 transport_http, origin, destination, max_paths):
    key = os.getenv("AMAP_API_KEY", "").strip()
    result = {"tool": "calculate_route", "call_id": call_id, "status": "error", "data": None,
              "evidence_refs": [], "warnings": [], "http_requests": 0}
    record = {"call_id": f"{call_id}:http:{index}", "operation": "amap." + operation,
              "query": query, "arguments": arguments, "observed_at": datetime.now(CHINA).isoformat(),
              "external_text_trust": "untrusted", "status": "error"}
    body = bytearray()
    started = time.perf_counter()
    error_type, retryable = "ConfigurationError", False
    try:
        if not key:
            raise ValueError("Missing map key")
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            raise TimeoutError()
        error_type = "SupplierResponseError"
        async with asyncio.timeout(remaining):
            async with httpx.AsyncClient(transport=transport_http, timeout=remaining,
                                         follow_redirects=False, trust_env=False) as client:
                result["http_requests"] = 1
                async with client.stream("GET", "https://restapi.amap.com" + path,
                                         params={**arguments, "key": key, "output": "JSON"}) as response:
                    record["http_status"] = response.status_code
                    response.raise_for_status()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > MAX_BYTES:
                            raise ValueError("Response size exceeded")
                        body.extend(chunk)
        payload = loads(body.decode("utf-8-sig"))
        route = payload.get("route") if isinstance(payload, dict) and payload.get("status") == "1" else None
        if not isinstance(route, dict) or not isinstance(route.get("paths"), list):
            raise ValueError("Invalid route response")
        if any(not isinstance(path_item, dict) for path_item in route["paths"]):
            raise ValueError("Invalid route path")
        paths = [_summarize_path(path_item, rank, call_id, route.get("taxi_cost"))
                 for rank, path_item in enumerate(route["paths"][:max_paths], 1)]
        result.update(status="ok", data={"origin": _endpoint(origin), "destination": _endpoint(destination),
            "origin_candidate_id": origin["candidate_id"], "destination_candidate_id": destination["candidate_id"],
            "provider_operation": "amap." + operation, "coordinate_system": "GCJ-02",
            "paths": paths, "returned_count": len(paths),
            "duration_basis": "query_time_estimate", "cost_basis": "tolls_and_taxi_fare_are_separate"})
        result["warnings"].append("驾车耗时是查询时估计；未知费用保持为空，通行费不作为出租车价格。")
        record["status"] = "ok"
    except (TimeoutError, httpx.TimeoutException):
        error_type, retryable = "RouteTimeout", True
    except httpx.HTTPStatusError as exc:
        error_type, retryable = "SupplierHTTPError", exc.response.status_code in (408, 429, 500, 502, 503, 504)
    except httpx.HTTPError:
        error_type, retryable = "RouteNetworkError", True
    except (ValueError, TypeError, UnicodeError, ArithmeticError):
        pass
    if result["status"] == "error":
        result["error"] = {"type": error_type, "message": "路线查询失败；未生成替代路线。", "retryable": retryable}
        record["error"] = result["error"]
    record["wire_text"] = body.decode("utf-8", errors="replace")
    record["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    secrets = (key, os.getenv("TUNIU_API_KEY", ""), os.getenv("DEEPSEEK_API_KEY", ""))
    record = redact(record, secrets)
    result = loads(dumps(redact(result, secrets)))
    if result["http_requests"]:
        result["evidence_refs"] = [record["call_id"]]
        try:
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"route-{call_id.rsplit(':', 1)[-1]}-http{index}.json"
            path.write_text(dumps(record) + "\n", encoding="utf-8")
        except OSError:
            result["evidence_error"] = {"type": "EvidenceWriteError",
                "message": "路线原始证据文件保存失败；查询结果及请求计数保留在 Trace 中。", "retryable": False}
            record["evidence_error"] = result["evidence_error"]
            result["warnings"].append(result["evidence_error"]["message"])
        else:
            record["source_ref"] = str(path)
    return result, record


def _summarize_path(path, rank, call_id, taxi_cost):
    cost = path.get("cost") if isinstance(path.get("cost"), dict) else {}
    duration = _positive_int(path.get("duration"))
    if duration is None:
        duration = _positive_int(cost.get("duration"))
    return {"route_id": f"{call_id}:path:{rank}", "supplier_route_id": path.get("route_id") or path.get("id"),
            "supplier_route_index": rank,
            "distance_meters": _positive_int(path.get("distance")),
            "duration_seconds": duration, "duration_basis": "query_time_estimate",
            "toll_cny": _money(cost.get("tolls")),
            "taxi_fare_cny": _money(taxi_cost if taxi_cost is not None else cost.get("taxi_cost")),
            "toll_distance_meters": _positive_int(cost.get("toll_distance")),
            "toll_road": cost.get("toll_road"), "restriction": path.get("restriction"),
            "steps_count": len(path.get("steps", [])) if isinstance(path.get("steps"), list) else None,
            "supplier_cost": cost}


def _positive_int(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _money(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not parsed.is_finite() or parsed < 0:
        return None
    return format(parsed, "f")
