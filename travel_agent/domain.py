from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
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
REAL_DRAFTS = {
    "no_feasible_route": "本次查询到的候选中未找到满足条件的完整方案；这不代表全市场没有可行方案。",
    "missing_data": "供应商证据不足，未生成完整方案；没有补造数据或切换到演示数据。",
}


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


def validate_verdict(value, seen, request, tool_results=None, tool_mode="demo", *, decisions=None):
    if tool_mode == "real":
        return _validate_real_verdict(value, seen, request, tool_results or {}, decisions or [])
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


def _parse_real_time(value, label, *, require_timezone=False):
    if not isinstance(value, str):
        raise ValueError(f"Invalid {label}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            if require_timezone:
                raise ValueError("Timezone required")
            parsed = parsed.replace(tzinfo=CHINA)
        return parsed.astimezone(CHINA)
    except (ValueError, OverflowError):
        raise ValueError(f"Invalid {label}") from None


def _real_index(tool_results):
    candidates, places, routes = {}, {}, {}
    for result in tool_results.values():
        if not isinstance(result, dict) or result.get("status") != "ok":
            continue
        data = result.get("data")
        if not isinstance(data, dict):
            continue
        tool = result.get("tool")
        if tool in {"search_transport", "search_stays", "resolve_place"}:
            for candidate in data.get("candidates", []):
                if not isinstance(candidate, dict) or not isinstance(candidate.get("candidate_id"), str):
                    continue
                index = places if tool == "resolve_place" else candidates
                identifier = candidate["candidate_id"]
                if identifier in index:
                    raise ValueError("Duplicate observed candidate ID")
                index[identifier] = (candidate, result, data)
        elif tool == "calculate_route":
            for path in data.get("paths", []):
                if not isinstance(path, dict) or not isinstance(path.get("route_id"), str):
                    continue
                identifier = path["route_id"]
                if identifier in routes:
                    raise ValueError("Duplicate observed route ID")
                routes[identifier] = (path, result, data)
    return candidates, places, routes


def _result_refs(result):
    refs = result.get("evidence_refs", []) if isinstance(result, dict) else []
    if not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs):
        raise ValueError("Invalid tool evidence references")
    return refs


def _selected_human_ids(decisions):
    identifiers = set()
    for decision in decisions:
        selected = decision.get("selected") if isinstance(decision, dict) else None
        candidate_id = selected.get("candidate_id") if isinstance(selected, dict) else None
        if isinstance(candidate_id, str):
            identifiers.add(candidate_id)
    return identifiers


def _place_projection(candidate):
    fields = ("candidate_id", "role", "place_id", "name", "address", "location",
              "coordinate_system", "source_candidate_id", "source_evidence_refs",
              "evidence_ref", "identity_confirmed", "match_confidence", "uncertainty_reasons")
    return {key: candidate.get(key) for key in fields}


def _validate_real_verdict(value, seen, request, tool_results, decisions):
    fields = {"status", "reason", "segment_ids", "evidence_ids"}
    if not isinstance(value, dict) or not fields.issubset(value):
        raise ValueError("Invalid verdict schema")
    reason = value["reason"]
    if (not isinstance(reason, str) or reason not in OUTCOMES or reason == "execution_failed"
            or value["status"] != OUTCOMES[reason]):
        raise ValueError("Inconsistent real verdict")
    allowed = fields | ({"planning"} if reason == "feasible" else set())
    if set(value) != allowed:
        raise ValueError("Invalid real verdict schema")
    evidence_ids = value["evidence_ids"]
    segment_ids = value["segment_ids"]
    if (not isinstance(evidence_ids, list) or len(evidence_ids) > 100
            or any(not isinstance(identifier, str) for identifier in evidence_ids)
            or len(set(evidence_ids)) != len(evidence_ids)
            or not set(evidence_ids).issubset(seen)):
        raise ValueError("Unobserved or duplicate evidence")
    if (not isinstance(segment_ids, list) or len(segment_ids) > 4
            or any(not isinstance(identifier, str) for identifier in segment_ids)
            or len(set(segment_ids)) != len(segment_ids)):
        raise ValueError("Invalid or duplicate itinerary segments")
    if reason != "feasible":
        if segment_ids:
            raise ValueError("A non-plan result cannot contain itinerary segments")
        if reason == "no_feasible_route":
            searches = [result for result in tool_results.values()
                        if isinstance(result, dict) and result.get("tool") in {"search_transport", "search_stays"}
                        and result.get("status") == "ok"]
            transport_searches = [result for result in searches if result["tool"] == "search_transport"
                and isinstance(result.get("data"), dict)
                and result["data"].get("origin") == request.get("origin")
                and result["data"].get("destination") == request.get("destination")
                and result["data"].get("departure_date") == request.get("required_arrival_date")
                and request.get("transport_mode", "any") in ("any", result["data"].get("mode"))]
            stay_searches = [result for result in searches if result["tool"] == "search_stays"
                and isinstance(result.get("data"), dict)
                and result["data"].get("destination") == request.get("destination")
                and result["data"].get("check_in") == request.get("required_arrival_date")
                and result["data"].get("check_out") == request.get("meeting_at", "")[:10]]
            if not transport_searches or not stay_searches:
                raise ValueError("A no-route conclusion requires correctly scoped transport and stay searches")
            transport_available = any(candidate.get("meets_request") is True
                and candidate.get("constraint_reasons") in ([], None)
                for result in transport_searches
                for candidate in result.get("data", {}).get("candidates", []))
            stay_available = any(result.get("data", {}).get("meets_request") is True
                and result.get("data", {}).get("candidates")
                for result in stay_searches)
            if transport_available and stay_available:
                raise ValueError("Observed candidates may still satisfy the request")
            search_refs = {ref for result in transport_searches + stay_searches for ref in _result_refs(result)}
            if not search_refs.issubset(set(evidence_ids)):
                raise ValueError("A no-route conclusion must cite both observed searches")
        return {**value, "tool_mode": "real", "itinerary": [], "total_cny": None,
                "required_evidence_ids": [], "scope_notes": []}

    if len(segment_ids) != 4:
        raise ValueError("A complete real plan requires four ordered segments")
    planning = value["planning"]
    if (not isinstance(planning, dict)
            or set(planning) != {"hotel_departure_at", "arrival_buffer_minutes"}
            or type(planning.get("arrival_buffer_minutes")) is not int
            or not 0 <= planning["arrival_buffer_minutes"] <= 1440):
        raise ValueError("Invalid planning values")

    candidates, places, routes = _real_index(tool_results)
    transport_entry = candidates.get(segment_ids[0])
    arrival_route_entry = routes.get(segment_ids[1])
    hotel_entry = candidates.get(segment_ids[2])
    meeting_route_entry = routes.get(segment_ids[3])
    if (transport_entry is None or transport_entry[1].get("tool") != "search_transport"
            or arrival_route_entry is None or hotel_entry is None
            or hotel_entry[1].get("tool") != "search_stays" or meeting_route_entry is None):
        raise ValueError("Selected itinerary segment was not observed")

    transport, transport_result, transport_query = transport_entry
    hotel, hotel_result, stay_query = hotel_entry
    if (transport_query.get("origin") != request.get("origin")
            or transport_query.get("destination") != request.get("destination")
            or transport.get("mode") not in {"flight", "train"}
            or request.get("transport_mode", "any") not in ("any", transport.get("mode"))
            or transport_query.get("mode") != transport.get("mode")
            or transport.get("meets_request") is not True
            or transport.get("constraint_reasons") not in ([], None)):
        raise ValueError("Transport candidate does not satisfy the request")
    departure_at = _parse_real_time(transport.get("departure_at"), "supplier departure time")
    transport_arrival = _parse_real_time(transport.get("arrival_at"), "supplier arrival time")
    if transport_arrival <= departure_at or transport_arrival.date().isoformat() != request.get("required_arrival_date"):
        raise ValueError("Transport must arrive on the required previous day")
    if transport_query.get("departure_date") != departure_at.date().isoformat():
        raise ValueError("Supplier departure time differs from its observed search date")

    meeting_at = _parse_real_time(request.get("meeting_at"), "meeting time")
    arrival_date = request.get("required_arrival_date")
    if (request.get("arrive_previous_day") is not True or request.get("stay_nights") != 1
            or stay_query.get("destination") != request.get("destination")
            or stay_query.get("check_in") != arrival_date
            or stay_query.get("check_out") != meeting_at.date().isoformat()
            or stay_query.get("meets_request") is not True
            or stay_query.get("constraint_reasons") not in ([], None)):
        raise ValueError("Hotel candidate does not establish the requested one-night stay")
    try:
        check_in = date.fromisoformat(stay_query["check_in"])
        check_out = date.fromisoformat(stay_query["check_out"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("Invalid supplier stay dates") from None
    if (check_out - check_in).days != 1:
        raise ValueError("Stay must be exactly one night")

    arrival_path, arrival_route_result, arrival_route = arrival_route_entry
    meeting_path, meeting_route_result, meeting_route = meeting_route_entry
    arrival_place_id = arrival_route.get("origin_candidate_id")
    hotel_place_id = arrival_route.get("destination_candidate_id")
    meeting_place_id = meeting_route.get("destination_candidate_id")
    if (arrival_route.get("origin", {}).get("candidate_id") != arrival_place_id
            or arrival_route.get("destination", {}).get("candidate_id") != hotel_place_id
            or meeting_route.get("origin", {}).get("candidate_id") != hotel_place_id
            or meeting_route.get("destination", {}).get("candidate_id") != meeting_place_id
            or arrival_route.get("origin", {}).get("role") != "arrival"
            or arrival_route.get("destination", {}).get("role") != "hotel"
            or meeting_route.get("origin", {}).get("role") != "hotel"
            or meeting_route.get("destination", {}).get("role") != "meeting"
            or meeting_route.get("origin_candidate_id") != hotel_place_id):
        raise ValueError("Disconnected or invalid route endpoints")
    if any(not isinstance(identifier, str) or identifier not in places
           for identifier in (arrival_place_id, hotel_place_id, meeting_place_id)):
        raise ValueError("Route references an unobserved place")
    arrival_place, arrival_place_result, arrival_place_query = places[arrival_place_id]
    hotel_place, hotel_place_result, hotel_place_query = places[hotel_place_id]
    meeting_place, meeting_place_result, meeting_place_query = places[meeting_place_id]
    selected_ids = _selected_human_ids(decisions)
    for candidate, result, query, role, source_id in (
            (arrival_place, arrival_place_result, arrival_place_query, "arrival", transport["candidate_id"]),
            (hotel_place, hotel_place_result, hotel_place_query, "hotel", hotel["candidate_id"]),
            (meeting_place, meeting_place_result, meeting_place_query, "meeting", None)):
        if (result.get("tool") != "resolve_place" or query.get("role") != role
                or candidate.get("role") != role or query.get("source_candidate_id") != source_id
                or candidate.get("source_candidate_id") != source_id):
            raise ValueError("Place is not bound to its selected source")
        uncertainty = candidate.get("uncertainty_reasons") or []
        if not isinstance(uncertainty, list):
            raise ValueError("Invalid place uncertainty")
        if uncertainty and (set(uncertainty) != {"multiple_candidates"}
                            or candidate["candidate_id"] not in selected_ids):
            raise ValueError("Ambiguous place requires a matching human selection")
    if meeting_place_query.get("keyword") != request.get("meeting_location"):
        raise ValueError("Meeting place differs from the original request")

    def checked_duration(path, route_data):
        seconds = path.get("duration_seconds")
        if (type(seconds) is not int or seconds <= 0
                or path.get("duration_basis") != "query_time_estimate"
                or route_data.get("duration_basis") != "query_time_estimate"):
            raise ValueError("Invalid observed route duration")
        return seconds

    arrival_seconds = checked_duration(arrival_path, arrival_route)
    meeting_seconds = checked_duration(meeting_path, meeting_route)
    hotel_arrival = transport_arrival + timedelta(seconds=arrival_seconds)
    if hotel_arrival.date().isoformat() != arrival_date:
        raise ValueError("Arrival transfer does not reach the hotel on the previous day")
    planned_departure = _parse_real_time(planning["hotel_departure_at"],
                                         "planned hotel departure", require_timezone=True)
    if planned_departure.date() != meeting_at.date():
        raise ValueError("Planned hotel departure must be on the meeting date")
    estimated_arrival = planned_departure + timedelta(seconds=meeting_seconds)
    buffered_arrival = estimated_arrival + timedelta(minutes=planning["arrival_buffer_minutes"])
    if buffered_arrival > meeting_at:
        raise ValueError("Buffered arrival is after the meeting start")

    required_refs = set()
    for result in (transport_result, arrival_route_result, hotel_result,
                   meeting_route_result, arrival_place_result, hotel_place_result, meeting_place_result):
        required_refs.update(_result_refs(result))
    for place in (arrival_place, hotel_place, meeting_place):
        refs = place.get("source_evidence_refs") or []
        if not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs):
            raise ValueError("Invalid place source evidence")
        required_refs.update(refs)
    if not required_refs.issubset(set(value["evidence_ids"])):
        raise ValueError("Complete plan is missing selected supplier evidence")

    def refs_for(results, *place_candidates):
        refs = {ref for result in results for ref in _result_refs(result)}
        for candidate in place_candidates:
            refs.update(candidate.get("source_evidence_refs") or [])
        return sorted(refs)

    transport_projection = {key: transport.get(key) for key in (
        "candidate_id", "mode", "flight_no", "train_no", "operator", "departure_at", "arrival_at",
        "departure_airport", "arrival_airport", "departure_station", "arrival_station")}
    arrival_projection = {key: arrival_path.get(key) for key in (
        "route_id", "distance_meters", "duration_seconds", "duration_basis", "toll_cny", "taxi_fare_cny")}
    meeting_projection = {key: meeting_path.get(key) for key in (
        "route_id", "distance_meters", "duration_seconds", "duration_basis", "toll_cny", "taxi_fare_cny")}
    itinerary = [
        {"kind": "transport", "candidate": transport_projection,
         "supplier_departure_at": departure_at.isoformat(timespec="seconds"),
         "supplier_arrival_at": transport_arrival.isoformat(timespec="seconds"),
         "evidence_refs": refs_for([transport_result])},
        {"kind": "arrival_to_hotel", "origin": _place_projection(arrival_place),
         "destination": _place_projection(hotel_place), "route": arrival_projection,
         "estimated_arrival_at": hotel_arrival.isoformat(timespec="seconds"),
         "time_basis": "supplier_arrival_plus_query_time_estimate",
         "evidence_refs": refs_for([arrival_route_result, arrival_place_result], arrival_place, hotel_place)},
        {"kind": "hotel", "candidate": {key: hotel.get(key) for key in (
             "candidate_id", "mode", "hotel_id", "name", "address", "price_basis")},
         "check_in": stay_query["check_in"], "check_out": stay_query["check_out"], "nights": 1,
         "adults": stay_query.get("adults"), "rooms": stay_query.get("rooms"),
         "evidence_refs": refs_for([hotel_result, hotel_place_result], hotel_place)},
        {"kind": "hotel_to_meeting", "origin": _place_projection(hotel_place),
         "destination": _place_projection(meeting_place), "route": meeting_projection,
         "planned_departure_at": planned_departure.isoformat(timespec="seconds"),
         "estimated_arrival_at": estimated_arrival.isoformat(timespec="seconds"),
         "arrival_buffer_minutes": planning["arrival_buffer_minutes"],
         "buffered_arrival_at": buffered_arrival.isoformat(timespec="seconds"),
         "meeting_at": meeting_at.isoformat(timespec="minutes"),
         "time_basis": "planned_departure_plus_query_time_estimate",
         "evidence_refs": refs_for([meeting_route_result, hotel_place_result, meeting_place_result],
                                   hotel_place, meeting_place)},
    ]
    scope_notes = [
        f"未提供{request['origin']}出发地具体地址，住址至机场/车站接驳及站内作业时间未纳入。",
        "抵达接驳按供应商抵达后立即出发估算，不含下机、出站、行李或等待时间。",
        "路线耗时是查询时估计，不保证未来路况；住宿查询日期不代表实际办理时刻。",
        "费用未知：真实交通、住宿与接驳费用未完整计价，未知项未计为零。",
        "地点来自本次供应商候选，未作实地或全面身份确认。",
        "本方案不作全面差旅合规或报销认证，且未添加演示预算或席别规则。",
    ]
    return {**value, "tool_mode": "real", "itinerary": itinerary, "total_cny": None,
            "required_evidence_ids": sorted(required_refs), "scope_notes": scope_notes}


def _duration_text(seconds):
    minutes, remainder = divmod(seconds, 60)
    return f"{minutes}分{remainder}秒" if remainder else f"{minutes}分钟"


def _real_draft(verdict):
    if verdict["status"] != "planned":
        return REAL_DRAFTS.get(verdict["reason"], DRAFTS.get(verdict["reason"], DRAFTS["execution_failed"]))
    transport, arrival, hotel, meeting = verdict["itinerary"]
    candidate = transport["candidate"]
    service = candidate.get("flight_no") or candidate.get("train_no") or candidate["candidate_id"]
    origin = candidate.get("departure_airport") or candidate.get("departure_station") or "出发地未细化"
    destination = candidate.get("arrival_airport") or candidate.get("arrival_station") or "到达地未细化"
    arrival_route, meeting_route = arrival["route"], meeting["route"]
    lines = [
        f"交通（供应商时刻，{service}）：{transport['supplier_departure_at'][:19].replace('T', ' ')} {origin} → "
        f"{transport['supplier_arrival_at'][:19].replace('T', ' ')} {destination}。证据：{', '.join(transport['evidence_refs'])}",
        f"前一日接驳（查询时估计）：{arrival['origin'].get('name')} → {arrival['destination'].get('name')}，"
        f"约 {_duration_text(arrival_route['duration_seconds'])}，预计 "
        f"{arrival['estimated_arrival_at'][:19].replace('T', ' ')} 到酒店。证据：{', '.join(arrival['evidence_refs'])}",
        f"住宿：{hotel['candidate'].get('name')}（{hotel['candidate'].get('address') or '地址未返回'}），"
        f"{hotel['check_in']} 至 {hotel['check_out']}，1 晚；具体办理时刻未由查询确认。"
        f"证据：{', '.join(hotel['evidence_refs'])}",
        f"次日到会：计划 {meeting['planned_departure_at'][:19].replace('T', ' ')} 离店；路线当前估计 "
        f"{_duration_text(meeting_route['duration_seconds'])}，预计 {meeting['estimated_arrival_at'][:19].replace('T', ' ')} 到达，"
        f"另留 {meeting['arrival_buffer_minutes']} 分钟缓冲，缓冲后 "
        f"{meeting['buffered_arrival_at'][:19].replace('T', ' ')}，会议 {meeting['meeting_at'].replace('T', ' ')}。"
        f"证据：{', '.join(meeting['evidence_refs'])}",
        "费用未知：未计算行程总价；未知费用没有按 ¥0 处理。",
    ]
    lines.extend(verdict["scope_notes"])
    return "\n".join(lines)


def format_draft(verdict, tool_mode="demo"):
    if tool_mode == "real":
        return _real_draft(verdict)
    if verdict["status"] != "planned":
        return DRAFTS[verdict["reason"]]
    return "\n".join(f'{x["start_at"][:16].replace("T", " ")} → {x["end_at"][:16].replace("T", " ")} '
                     f'{x["from"]} → {x["to"]} · {x["label"]} · ¥{x["price_cny"]}'
                     for x in verdict["itinerary"]) + f'\n模拟总费用：¥{verdict["total_cny"]}（人民币）'
