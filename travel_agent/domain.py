from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

PACKAGE = Path(__file__).parent
CHINA = timezone(timedelta(hours=8))
OUTCOMES = {"feasible": "planned", "no_feasible_route": "unavailable",
            "missing_data": "needs_review", "execution_failed": "needs_review"}
DRAFTS = {"no_feasible_route": "当前演示数据中没有满足时间与政策的完整方案，请调整条件后重试。",
          "missing_data": "必要的工具数据暂时不可用，未生成方案，请稍后重试。",
          "execution_failed": "规划未能完成，请查看调用链中的终止原因与错误。"}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def scenarios():
    return json.loads((PACKAGE / "data/scenarios.json").read_text(encoding="utf-8"))


def scenario_by_id(identifier):
    for item in scenarios():
        if item["id"] == identifier:
            return item
    raise ValueError("Unknown scenario")


def normalize_request(value, tool_mode="demo"):
    fields = {"origin", "destination", "meeting_location", "meeting_at"}
    if not isinstance(value, dict) or not fields.issubset(value) or set(value) - fields - {"transport_mode", "arrive_previous_day"}:
        raise ValueError("Expected origin, destination, meeting_location and meeting_at")
    if any(not isinstance(v, str) or not v.strip() or len(v) > 120 for v in (value[k] for k in fields)):
        raise ValueError("Invalid request field")
    if "transport_mode" in value and value["transport_mode"] not in ("flight", "train", "any"):
        raise ValueError("Invalid transport mode")
    if "arrive_previous_day" in value and type(value["arrive_previous_day"]) is not bool:
        raise ValueError("Invalid previous-day arrival requirement")
    result = {k: v.strip() if isinstance(v, str) else v for k, v in value.items()}
    try:
        meeting = datetime.fromisoformat(result["meeting_at"])
        if meeting.tzinfo is None:
            meeting = meeting.replace(tzinfo=CHINA)
        meeting = meeting.astimezone(CHINA)
        previous = meeting.date() - timedelta(days=1)
    except (ValueError, OverflowError) as exc:
        raise ValueError("Invalid meeting date or timezone") from exc
    result["meeting_at"] = meeting.isoformat(timespec="minutes")
    if tool_mode == "real":
        result.setdefault("transport_mode", "any")
        result.setdefault("arrive_previous_day", True)
        if result["arrive_previous_day"]:
            result["required_arrival_date"] = str(previous)
            result["stay_nights"] = 1
    else:
        result["earliest_departure"] = f"{previous}T18:00+08:00"
    return result


def custom_scenario(request):
    normalized = normalize_request(request)
    # Store the original four validated fields; normalize again at the loop boundary.
    return {"id": "custom-" + fingerprint(normalized)[:8],
            "request": {k: normalized[k] for k in request}}


def handoff():
    return {"status": "needs_review", "reason": "execution_failed", "segment_ids": [],
            "evidence_ids": [], "itinerary": [], "total_cny": None}


def validate_verdict(value, seen, request, tool_results=None, tool_mode="demo"):
    if not isinstance(value, dict) or set(value) != {"status", "reason", "segment_ids", "evidence_ids"}:
        raise ValueError("Invalid verdict schema")
    reason = value["reason"]
    if not isinstance(reason, str) or reason not in OUTCOMES or reason == "execution_failed":
        raise ValueError("Invalid reason")
    if value["status"] != OUTCOMES[reason]:
        raise ValueError("Inconsistent status")
    for key in ("segment_ids", "evidence_ids"):
        ids = value[key]
        if (not isinstance(ids, list) or len(ids) > 20 or any(not isinstance(x, str) for x in ids)
                or len(set(ids)) != len(ids) or not set(ids).issubset(seen)):
            raise ValueError("Unobserved or duplicate evidence")
    if tool_mode == "real":
        if reason != "missing_data" or value["segment_ids"]:
            raise ValueError("Partial evidence cannot establish a travel plan or market unavailability")
        return {**value, "itinerary": [], "total_cny": None}
    if reason != "feasible":
        required = {"get_policy", "search_intercity", "search_hotels", "search_local_transport"}
        if tool_results is None or not required.issubset(tool_results):
            raise ValueError("Failure conclusion requires all four queries")
        missing = any(result.get("error") for result in tool_results.values())
        if (reason == "missing_data") != missing:
            raise ValueError("Failure reason does not match tool availability")
        if value["segment_ids"]:
            raise ValueError("Failed planning must not contain itinerary")
        return {**value, "itinerary": [], "total_cny": None}
    refs = value["evidence_ids"]
    segments = [seen[x] for x in value["segment_ids"]]
    if not set(value["segment_ids"]).issubset(refs):
        raise ValueError("Missing segment citations")
    if [x.get("kind") for x in segments] != ["intercity", "station_to_hotel", "hotel", "hotel_to_meeting"]:
        raise ValueError("Incomplete or unordered itinerary")
    policies = [seen[x] for x in refs if seen[x].get("source") == "get_policy"]
    if len(policies) != 1:
        raise ValueError("Policy citation required")
    policy = policies[0]
    rail, transfer, hotel, last = segments
    if [x.get("source") for x in segments] != ["search_intercity", "search_local_transport", "search_hotels", "search_local_transport"]:
        raise ValueError("Wrong source")
    if rail["origin_city"] != request["origin"] or rail["destination_city"] != request["destination"]:
        raise ValueError("Wrong cities")
    if (rail["to"] != transfer["from"] or transfer["to"] != hotel["from"]
            or hotel["from"] != hotel["to"] or hotel["to"] != last["from"]
            or last["to"] != request["meeting_location"]):
        raise ValueError("Disconnected locations")
    times = [(datetime.fromisoformat(x["start_at"]), datetime.fromisoformat(x["end_at"])) for x in segments]
    meeting = datetime.fromisoformat(request["meeting_at"])
    earliest = datetime.fromisoformat(request["earliest_departure"])
    if times[0][0] < earliest or times[0][0].date() != earliest.date():
        raise ValueError("Must depart the previous evening")
    if any(start >= end for start, end in times) or any(times[i][1] > times[i+1][0] for i in range(3)):
        raise ValueError("Overlapping or reversed times")
    if times[-1][1] > meeting - timedelta(minutes=policy["arrival_buffer_minutes"]):
        raise ValueError("Arrival too late")
    if rail["seat_class"] != policy["rail_class"] or any(x["mode"] not in policy["local_modes"] for x in (transfer, last)):
        raise ValueError("Unsupported travel class or mode")
    costs = [x["price_cny"] for x in segments]
    if any(type(x) is not int or x < 0 for x in costs):
        raise ValueError("Invalid price")
    total = sum(costs)
    if hotel["price_cny"] > policy["hotel_max_cny"] or total > policy["trip_max_cny"]:
        raise ValueError("Over budget")
    return {**value, "itinerary": segments, "total_cny": total}


def format_draft(verdict):
    if verdict["status"] != "planned":
        return DRAFTS[verdict["reason"]]
    return "\n".join(f'{x["start_at"][:16].replace("T", " ")} → {x["end_at"][:16].replace("T", " ")} '
                     f'{x["from"]} → {x["to"]} · {x["label"]} · ¥{x["price_cny"]}'
                     for x in verdict["itinerary"]) + f'\n模拟总费用：¥{verdict["total_cny"]}（人民币）'
