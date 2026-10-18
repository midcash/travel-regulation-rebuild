"""Bounded read-only transport queries with per-call supplier evidence."""
import asyncio
from datetime import date, datetime
import json
import os
import time
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from .facts import CHINA
from .supplier_wire import complete_sse, decode_wire, dumps, redact, unpack
from .transport_summary import _flight_summary, _train_summary, _is_high_speed

MAX_BYTES = 16 * 1024 * 1024
SEARCH_NAMES = {"flight": "searchLowestPriceFlight", "train": "searchLowestPriceTrain"}


def validate_search(args):
    required = {"mode", "origin", "destination", "departure_date"}
    if not isinstance(args, dict) or not required.issubset(args) or set(args) - required - {"service_class", "max_results"}:
        raise ValueError("Invalid transport arguments")
    if args["mode"] not in ("flight", "train"):
        raise ValueError("Invalid transport mode")
    for key in ("origin", "destination", "departure_date"):
        if not isinstance(args[key], str) or not args[key].strip() or len(args[key]) > 120:
            raise ValueError("Invalid transport field")
    if date.fromisoformat(args["departure_date"]).isoformat() != args["departure_date"]:
        raise ValueError("Expected ISO departure date")
    if args.get("service_class", "any") not in ("any", "high_speed_rail"):
        raise ValueError("Invalid service class")
    count = args.get("max_results", 5)
    if type(count) is not int or not 1 <= count <= 20:
        raise ValueError("max_results must be 1..20")


def assess_candidate(candidate, query, request):
    """Keep supplier facts unchanged; annotate requested constraints separately."""
    reasons = []
    if (query["origin"], query["destination"]) != (request["origin"], request["destination"]):
        reasons.append("wrong_cities")
    if request["transport_mode"] not in ("any", candidate["mode"]):
        reasons.append("wrong_mode")
    try:
        times = []
        for key in ("departure_at", "arrival_at"):
            value = candidate[key]
            if not isinstance(value, str) or len(value) < 16:
                raise ValueError("Incomplete supplier time")
            parsed = datetime.fromisoformat(value)
            times.append((parsed.replace(tzinfo=CHINA) if parsed.tzinfo is None else parsed).astimezone(CHINA))
        departure, arrival = times
        if departure.date().isoformat() != query["departure_date"]:
            reasons.append("wrong_departure_date")
        if arrival <= departure:
            reasons.append("reversed_times")
        if request.get("required_arrival_date") and arrival.date().isoformat() != request["required_arrival_date"]:
            reasons.append("not_previous_day_arrival")
        if arrival > datetime.fromisoformat(request["meeting_at"]):
            reasons.append("after_meeting")
    except (ValueError, TypeError, OverflowError):
        reasons.append("unknown_or_invalid_times")
    candidate["meets_request"] = not reasons
    candidate["constraint_reasons"] = reasons


async def search_transport(args, request, call_id, folder, timeout, transport_http=None):
    validate_search(args)
    mode = args["mode"]
    def summarize(payloads):
        warnings = []
        payload = next((p for p in payloads if isinstance(p, dict) and isinstance(p.get("data"), list)), None)
        if payload is None or any(not isinstance(row, dict) for row in payload["data"]):
            raise ValueError("Invalid supplier rows")
        rows = payload["data"]
        if mode == "train" and args.get("service_class") == "high_speed_rail":
            rows = [row for row in rows if _is_high_speed(row)]
            warnings.append("high_speed_rail filtering is applied locally")
        candidates = []
        for index, row in enumerate(rows[:args.get("max_results", 5)], 1):
            candidate = (_flight_summary if mode == "flight" else _train_summary)(row, f"{call_id}:http:1")
            candidate["supplier_candidate_id"] = candidate["candidate_id"]
            candidate["candidate_id"] = f"{call_id}:candidate:{index}"
            assess_candidate(candidate, args, request)
            candidates.append(candidate)
        return {**args, "candidates": candidates, "returned_count": len(candidates),
                "supplier_total_pages": payload.get("totalPageNum")}, warnings

    return await query_transport(mode, SEARCH_NAMES[mode], {
        "departureCityName": args["origin"], "arrivalCityName": args["destination"],
        "departureDate": args["departure_date"]}, args, "search_transport", call_id,
        folder, timeout, transport_http, summarize)


async def query_transport(mode, operation, arguments, query, tool, call_id,
                          folder, timeout, transport_http, summarize):
    key = os.getenv("TUNIU_API_KEY", "").strip()
    endpoint = os.getenv(f"TUNIU_{mode.upper()}_URL", f"https://openapi.tuniu.cn/mcp/{mode}")
    result = {"tool": tool, "call_id": call_id, "status": "error", "data": None,
              "evidence_refs": [], "warnings": [], "http_requests": 0}
    record = {"call_id": f"{call_id}:http:1", "operation": operation,
              "query": query, "observed_at": datetime.now(CHINA).isoformat(), "status": "error",
              "external_text_trust": "untrusted"}
    error_type, retryable = "ConfigurationError", False
    body = bytearray()
    started = time.perf_counter()
    try:
        url = urlsplit(endpoint)
        if not key or url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("Invalid supplier configuration")
        rpc_id = uuid4().hex
        wire_request = {"jsonrpc": "2.0", "id": rpc_id, "method": "tools/call", "params": {
            "name": operation, "arguments": arguments}}
        error_type = "SupplierResponseError"
        async with asyncio.timeout(min(45, timeout)):
            async with httpx.AsyncClient(transport=transport_http, timeout=min(45, timeout),
                                         follow_redirects=False, trust_env=False) as client:
                result["http_requests"] = 1
                async with client.stream("POST", endpoint, json=wire_request,
                        headers={"apiKey": key, "Accept": "application/json, text/event-stream"}) as response:
                    record["http_status"] = response.status_code
                    response.raise_for_status()
                    is_sse = "text/event-stream" in response.headers.get("content-type", "")
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > MAX_BYTES:
                            raise ValueError("Response size exceeded")
                        body.extend(chunk)
                        if is_sse:
                            events, _ = complete_sse(bytes(body))
                            if any(isinstance(e, dict) and e.get("id") == rpc_id and
                                   ("result" in e or "error" in e) for e in events):
                                break
        envelopes = complete_sse(bytes(body))[0] if is_sse else decode_wire(bytes(body), False)
        matches = [e for e in envelopes if isinstance(e, dict) and e.get("id") == rpc_id]
        if len(matches) != 1 or matches[0].get("jsonrpc") != "2.0":
            raise ValueError("Invalid RPC response")
        envelope = matches[0]
        if "error" in envelope or not isinstance(envelope.get("result"), dict) or envelope["result"].get("isError"):
            raise ValueError("Supplier RPC failure")
        payloads = unpack(envelope)
        if any(isinstance(p, dict) and (p.get("success") is False or p.get("successCode") is False) for p in payloads):
            raise ValueError("Supplier business failure")
        data, warnings = summarize(payloads)
        result.update(status="ok", data=data, warnings=warnings)
        record["status"] = "ok"
    except (TimeoutError, httpx.TimeoutException):
        error_type, retryable = "TransportTimeout", True
    except httpx.HTTPStatusError as exc:
        error_type, retryable = "SupplierHTTPError", exc.response.status_code in (408, 429, 500, 502, 503, 504)
    except httpx.HTTPError:
        error_type, retryable = "TransportNetworkError", True
    except (ValueError, TypeError, UnicodeError, ArithmeticError):
        pass
    if result["status"] == "error":
        result["error"] = {"type": error_type, "message": "供应商查询未取得有效结果；未生成替代数据。", "retryable": retryable}
        record["error"] = result["error"]
    record["wire_text"] = body.decode("utf-8", errors="replace")
    record["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    secrets = (key, os.getenv("DEEPSEEK_API_KEY", ""))
    record = redact(record, secrets)
    result = json.loads(dumps(redact(result, secrets)))
    if result["http_requests"]:
        result["evidence_refs"] = [record["call_id"]]
        try:
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"transport-{call_id.rsplit(':', 1)[-1]}.json"
            path.write_text(dumps(record) + "\n", encoding="utf-8")
        except OSError:
            # Keep the supplier outcome for the Trace; a failed file is not a source.
            result["evidence_error"] = {"type": "EvidenceWriteError",
                "message": "供应商原始证据文件保存失败；查询结果及请求计数保留在 Trace 中。", "retryable": False}
            record["evidence_error"] = result["evidence_error"]
            result["warnings"].append(result["evidence_error"]["message"])
        else:
            record["source_ref"] = str(path)
    return result, record
