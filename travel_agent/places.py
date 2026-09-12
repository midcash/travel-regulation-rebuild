"""Evidence-bound place lookup; supplier ranking never confirms an identity."""
import asyncio
from datetime import datetime
import os
import re
import time

import httpx

from .domain import CHINA
from .supplier_wire import loads, dumps, redact
from .transport import MAX_BYTES


def _airport_key(value):
    """Reduce supplier airport labels for a conservative same-airport check."""
    if not isinstance(value, str):
        return ""
    value = value.replace("机场", "").replace("航站楼", "")
    value = re.sub(r"t\d+", "", value, flags=re.I)
    return "".join(value.split()).lower()


async def resolve_place(args, context, call_id, folder, timeout, transport_http=None):
    request, history = context
    if not isinstance(args, dict) or set(args) - {"role", "candidate_id", "city", "keyword", "max_results"}:
        raise ValueError("Invalid place arguments")
    role = args.get("role")
    if role not in {"meeting", "arrival", "hotel"}:
        raise ValueError("Expected place role")
    limit = args.get("max_results", 5)
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError("max_results must be 1..20")
    city, keyword = request["destination"], request["meeting_location"]
    refs, address, source_id, source_conflicts = [], None, None, []
    if role == "meeting":
        if "candidate_id" in args:
            raise ValueError("Meeting comes from request")
    else:
        source = "search_transport" if role == "arrival" else "search_stays"
        source_id = args.get("candidate_id")
        if not isinstance(source_id, str):
            raise ValueError("Observed candidate required")
        matches = [(r["data"], c) for r in history.values() if r["tool"] == source and r["status"] == "ok"
                   for c in r["data"]["candidates"] if c["candidate_id"] == source_id]
        if len(matches) != 1:
            raise ValueError("Unobserved place source")
        original, candidate = matches[0]
        city = original["destination"]
        refs.append(candidate["evidence_ref"])
        detail_name = "get_transport_detail" if role == "arrival" else "get_stay_detail"
        details = [r for r in history.values() if r["tool"] == detail_name and r["status"] == "ok"
                   and r["data"]["candidate_id"] == source_id]
        detail = details[-1] if details else None
        if detail:
            refs.extend(detail["evidence_refs"])
        if role == "hotel":
            keyword, address = candidate.get("name"), candidate.get("address")
        elif candidate["mode"] == "flight":
            info = (detail["data"].get("flight") or {}) if detail else {}
            candidate_airport = candidate.get("arrival_airport")
            detail_airport = info.get("arrivalAirport")
            if (candidate_airport and detail_airport
                    and not (_airport_key(candidate_airport) in _airport_key(detail_airport)
                             or _airport_key(detail_airport) in _airport_key(candidate_airport))):
                source_conflicts.append("arrival_airport_detail_mismatch")
            # Keep the selected search candidate as the identity anchor when detail disagrees.
            keyword = candidate_airport or detail_airport
            if isinstance(keyword, str) and "机场" not in keyword:
                keyword += "机场"
            terminal = info.get("arrivalTerminal")
            if isinstance(keyword, str) and isinstance(terminal, str) and terminal and terminal not in keyword:
                keyword += terminal
        else:
            keyword = candidate.get("arrival_station")
    if args.get("city", city) != city or args.get("keyword", keyword) != keyword:
        raise ValueError("Place differs from request")
    query = {"role": role, "city": city, "keyword": keyword, "source_evidence_refs": refs,
             "source_candidate_id": source_id, "source_address": address,
             "source_conflicts": source_conflicts}
    if not isinstance(keyword, str) or not keyword.strip() or len(keyword) > 80:
        return {"tool": "resolve_place", "call_id": call_id, "status": "error", "data": None,
                "evidence_refs": [], "warnings": [], "http_requests": 0, "error": {
                    "type": "MissingCandidateData", "message": "来源缺少可查询的地点名称。", "retryable": False}}, {}
    deadline = time.perf_counter() + min(45, timeout)
    result, record = await query_map("poi_search", "/v5/place/text", {
        "keywords": keyword, "region": city, "city_limit": "true", "page_size": limit,
        "page_num": 1, "show_fields": "children,business,indoor,navi,photos"}, "pois",
        call_id, 1, query, folder, deadline, transport_http)
    records = [record]
    if result["status"] != "ok":
        return result, {"requests": records}
    candidates = []
    for index, row in enumerate(result["data"][:limit], 1):
        location = row.get("location")
        reasons = []
        try:
            longitude, latitude = [float(v) for v in location.split(",")]
            if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
                raise ValueError("Invalid coordinate")
        except (AttributeError, ValueError, TypeError):
            reasons.append("invalid_coordinates")
        name = row.get("name")
        if not isinstance(name, str) or name.strip() != keyword.strip():
            reasons.append("name_not_exact")
        if row.get("cityname") not in (city, city + "市"):
            reasons.append("city_not_verified")
        if address and row.get("address") != address:
            reasons.append("address_not_exact")
        if len(result["data"]) > 1:
            reasons.append("multiple_candidates")
        reasons.extend(source_conflicts)
        candidates.append({"candidate_id": f"{call_id}:candidate:{index}", "place_id": row.get("id"),
            "name": name, "address": row.get("address"), "supplier_address": row.get("address"),
            "address_source": "supplier" if isinstance(row.get("address"), str) and row["address"].strip() else None,
            "location": location,
            "coordinate_system": "GCJ-02", "city": row.get("cityname"), "district": row.get("adname"),
            "source_candidate_id": source_id, "source_evidence_refs": refs,
            "evidence_ref": record["call_id"], "identity_confirmed": False,
            "match_confidence": "uncertain" if reasons else "supplier_ranked",
            "uncertainty_reasons": reasons, "supplier": row})
    # Keep the experiment's first-result enrichments as separate evidence, never as coordinate replacements.
    if candidates and isinstance(candidates[0]["place_id"], str) and candidates[0]["place_id"]:
        first = candidates[0]
        for index, (operation, path, arguments, expected, field) in enumerate([
            ("poi_detail", "/v5/place/detail", {"id": first["place_id"], "show_fields": "children,business,indoor,navi,photos"}, "pois", "detail"),
            ("geocode", "/v3/geocode/geo", {"address": city + str(first["address"] or "") + str(first["name"] or ""), "city": city}, "geocodes", "geocode")], 2):
            extra, record = await query_map(operation, path, arguments, expected, call_id, index,
                                             query, folder, deadline, transport_http)
            records.append(record)
            result["http_requests"] += extra["http_requests"]
            result["evidence_refs"].extend(extra["evidence_refs"])
            if extra.get("evidence_error"):
                result["evidence_error"] = extra["evidence_error"]
            result["warnings"].extend(extra["warnings"])
            if extra["status"] == "ok":
                rows = extra["data"]
                if len(rows) == 1 and (field != "detail" or rows[0].get("id") == first["place_id"]):
                    first[field] = rows[0]
                    first[field + "_evidence_ref"] = record["call_id"]
                    if not first["address"] and field in {"detail", "geocode"}:
                        formatted = rows[0].get("formatted_address") if field == "geocode" else rows[0].get("address")
                        if isinstance(formatted, str) and formatted.strip():
                            first["address"] = formatted
                            first["address_source"] = field
                    if rows[0].get("location") and rows[0]["location"] != first["location"]:
                        first["uncertainty_reasons"].append(field + "_coordinates_differ")
                else:
                    first["uncertainty_reasons"].append(field + "_identity_unresolved")
            else:
                first[field + "_error"] = extra["error"]
                first["uncertainty_reasons"].append(field + "_unavailable")
            if first["uncertainty_reasons"]:
                first["match_confidence"] = "uncertain"
        if not isinstance(first["address"], str) or not first["address"].strip():
            first["uncertainty_reasons"].append("address_unavailable")
            first["match_confidence"] = "uncertain"
    result["data"] = {**query, "candidates": candidates, "identity_confirmed": False, "returned_count": len(candidates)}
    result["warnings"].append("地点为供应商候选，未经人工确认；补充地理编码不替换候选坐标。")
    return result, {"requests": records}


async def query_map(operation, path, arguments, expected, call_id, index, query, folder, deadline, transport_http):
    key = os.getenv("AMAP_API_KEY", "").strip()
    result = {"tool": "resolve_place", "call_id": call_id, "status": "error", "data": None,
              "evidence_refs": [], "warnings": [], "http_requests": 0}
    record = {"call_id": f"{call_id}:http:{index}", "operation": "amap." + operation, "query": query,
              "arguments": arguments, "observed_at": datetime.now(CHINA).isoformat(),
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
        if not isinstance(payload, dict) or payload.get("status") != "1" or not isinstance(payload.get(expected), list):
            raise ValueError("Invalid place response")
        if any(not isinstance(row, dict) for row in payload[expected]):
            raise ValueError("Invalid place row")
        result.update(status="ok", data=payload[expected])
        record["status"] = "ok"
    except (TimeoutError, httpx.TimeoutException):
        error_type, retryable = "PlaceTimeout", True
    except httpx.HTTPStatusError as exc:
        error_type, retryable = "SupplierHTTPError", exc.response.status_code in (408, 429, 500, 502, 503, 504)
    except httpx.HTTPError:
        error_type, retryable = "PlaceNetworkError", True
    except (ValueError, TypeError, UnicodeError, ArithmeticError):
        pass
    if result["status"] == "error":
        result["error"] = {"type": error_type, "message": "地点查询失败；未生成替代地点。", "retryable": retryable}
        record["error"] = result["error"]
    record["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    secrets = (key, os.getenv("TUNIU_API_KEY", ""), os.getenv("DEEPSEEK_API_KEY", ""))
    record, result = redact(record, secrets), loads(dumps(redact(result, secrets)))
    if result["http_requests"]:
        result["evidence_refs"] = [record["call_id"]]
        try:
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"place-{call_id.rsplit(':', 1)[-1]}-http{index}.json"
            path.write_text(dumps({**record, "wire_text": redact(body.decode("utf-8", errors="replace"), secrets)}) + "\n", encoding="utf-8")
        except OSError:
            result["evidence_error"] = {"type": "EvidenceWriteError", "message": "地点原始证据保存失败。", "retryable": False}
            result["warnings"].append(result["evidence_error"]["message"])
        else:
            record["source_ref"] = str(path)
    return result, record
