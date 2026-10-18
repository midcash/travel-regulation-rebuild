"""Observed hotel candidates and date-bound room quotes."""
from datetime import date

from .transport import query_transport


async def search_stays(args, request, call_id, folder, timeout, transport_http=None):
    required = {"destination", "check_in", "check_out"}
    if not isinstance(args, dict) or not required <= args.keys() or args.keys() - required - {"meeting_place", "max_results"}:
        raise ValueError("Invalid stay search")
    for key in required | ({"meeting_place"} if "meeting_place" in args else set()):
        if not isinstance(args[key], str) or not 1 <= len(args[key].strip()) <= 200:
            raise ValueError("Invalid stay field")
    start, end = date.fromisoformat(args["check_in"]), date.fromisoformat(args["check_out"])
    if start.isoformat() != args["check_in"] or end.isoformat() != args["check_out"] or end <= start:
        raise ValueError("Invalid stay dates")
    limit = args.get("max_results", 5)
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError("max_results must be 1..20")
    arguments = {"cityName": args["destination"], "checkIn": args["check_in"], "checkOut": args["check_out"], "adultNum": 1}
    if args.get("meeting_place"):
        arguments["poiName"] = args["meeting_place"]
    reasons = []
    if args["destination"] != request["destination"]:
        reasons.append("wrong_destination")
    if request.get("required_arrival_date") and (args["check_in"] != request["required_arrival_date"]
            or args["check_out"] != request["meeting_at"][:10]):
        reasons.append("wrong_stay_dates")

    def summarize(payloads):
        payload = next((p for p in payloads if isinstance(p, dict) and "hotels" in p), None)
        if payload is None or not isinstance(payload["hotels"], list) or any(not isinstance(r, dict) for r in payload["hotels"]):
            raise ValueError("Invalid hotel rows")
        candidates = []
        for index, row in enumerate(payload["hotels"][:limit], 1):
            identifier = row.get("hotelId")
            if identifier is not None and (type(identifier) not in (str, int) or not str(identifier).strip()):
                raise ValueError("Invalid hotel ID")
            candidates.append({"candidate_id": f"{call_id}:candidate:{index}", "mode": "hotel",
                "hotel_id": identifier, "name": row.get("hotelName"), "address": row.get("address"),
                "lowest_price_raw": row.get("lowestPrice"), "price_basis": "unknown", "supplier": row,
                "evidence_ref": f"{call_id}:http:1"})
        return {**args, "adults": 1, "rooms": 1, "candidates": candidates,
                "meets_request": not reasons, "constraint_reasons": reasons,
                "returned_count": len(candidates), "supplier_total_pages": payload.get("totalPageNum")}, [
                    "暂按1成人、1间房查询；日期为住宿查询条件，不是实际办理入住时间；价格口径未知，不作为整段总价。"]

    return await query_transport("hotel", "tuniuHotelSearch", arguments, args, "search_stays", call_id,
                                 folder, timeout, transport_http, summarize)


async def get_stay_detail(args, history, call_id, folder, timeout, transport_http=None):
    if not isinstance(args, dict) or set(args) != {"candidate_id"} or not isinstance(args["candidate_id"], str):
        raise ValueError("Expected observed hotel candidate")
    matches = [(r["data"], c) for r in history.values() if r["tool"] == "search_stays" and r["status"] == "ok"
               for c in r["data"]["candidates"] if c["candidate_id"] == args["candidate_id"]]
    if len(matches) != 1:
        raise ValueError("Unobserved or ambiguous hotel")
    query, candidate = matches[0]
    identifier = candidate["hotel_id"]
    if identifier is None:
        return {"tool": "get_stay_detail", "call_id": call_id, "status": "error", "data": None,
                "evidence_refs": [], "warnings": [], "http_requests": 0, "error": {
                    "type": "MissingCandidateData", "message": "酒店候选缺少供应商ID。", "retryable": False}}, {}
    provider_id = int(identifier) if str(identifier).isascii() and str(identifier).isdigit() else identifier
    arguments = {"hotelId": provider_id, "checkIn": query["check_in"], "checkOut": query["check_out"],
                 "roomNum": query["rooms"], "adultNum": query["adults"]}

    def summarize(payloads):
        payload = next((p for p in payloads if isinstance(p, dict) and "roomTypes" in p), None)
        if payload is None or not isinstance(payload["roomTypes"], list):
            raise ValueError("Invalid hotel detail")
        returned_id = payload.get("hotelId")
        if returned_id is not None and str(returned_id) not in {str(identifier), str(provider_id)}:
            raise ValueError("Supplier returned another hotel")
        for key in ("checkIn", "checkOut"):
            if payload.get(key) is not None and payload[key] != arguments[key]:
                raise ValueError("Supplier returned other stay dates")
        for room in payload["roomTypes"]:
            if not isinstance(room, dict) or not isinstance(room.get("ratePlans", []), list) or any(not isinstance(r, dict) for r in room.get("ratePlans", [])):
                raise ValueError("Invalid room rates")
        return {"candidate_id": candidate["candidate_id"], "candidate_evidence_ref": candidate["evidence_ref"],
                "check_in": query["check_in"], "check_out": query["check_out"], "adults": query["adults"], "rooms": query["rooms"],
                "price_basis": "unknown", "hotel": {k: v for k, v in payload.items() if k != "roomTypes"},
                "room_types": payload["roomTypes"], "returned_count": len(payload["roomTypes"])}, [
                    "房型与报价保留供应商原值；价格口径未知，不计算整段总价；住宿日期不代表实际办理入住时间。"]

    return await query_transport("hotel", "tuniuHotelDetail", arguments, args, "get_stay_detail", call_id,
                                 folder, timeout, transport_http, summarize)
