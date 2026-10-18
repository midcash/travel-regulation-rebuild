"""Resolve detail queries exclusively from this run's observed candidates."""
from datetime import datetime

from .domain import CHINA
from .transport import query_transport


async def get_transport_detail(args, history, call_id, folder, timeout, transport_http=None):
    if (not isinstance(args, dict) or set(args) - {"candidate_id", "max_options"}
            or not isinstance(args.get("candidate_id"), str) or not args["candidate_id"]):
        raise ValueError("Expected observed candidate_id")
    limit = args.get("max_options", 20)
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError("max_options must be 1..20")
    matches = [(r["data"], c) for r in history.values()
               if r["tool"] == "search_transport" and r["status"] == "ok"
               for c in r["data"]["candidates"] if c["candidate_id"] == args["candidate_id"]]
    if len(matches) != 1:
        raise ValueError("Unobserved or ambiguous candidate")
    query, candidate = matches[0]
    mode = candidate["mode"]
    if mode == "flight":
        operation = "multiCabinDetails"
        arguments = {"departureCityName": query["origin"], "arrivalCityName": query["destination"],
                     "departureDate": query["departure_date"], "flightNo": candidate["flight_no"]}
    else:
        operation = "queryTrainDetail"
        arguments = {"departureStationName": candidate["departure_station"],
                     "arrivalStationName": candidate["arrival_station"],
                     "departureDate": query["departure_date"], "trainNum": candidate["train_no"]}
    if not all(isinstance(v, str) and v.strip() for v in arguments.values()):
        return {"tool": "get_transport_detail", "call_id": call_id, "status": "error", "data": None,
                "evidence_refs": [], "warnings": [], "http_requests": 0,
                "error": {"type": "MissingCandidateData", "message": "候选缺少查询详情所需的班次或站名。",
                          "retryable": False}}, {}

    def summarize(payloads):
        if mode == "flight":
            payload = next((p for p in payloads if isinstance(p, dict) and "cabinInfo" in p), None)
            if payload is None:
                raise ValueError("Missing cabinInfo")
            info, options = payload.get("flightInfo"), payload["cabinInfo"]
            if isinstance(info, list):
                if len(info) > 1:
                    raise ValueError("Ambiguous flight detail")
                info = info[0] if info else None
            fields = ("cabinPriceId", "basePrice", "totalTax", "cabinClass", "cabinClassCodes",
                      "buyCondition", "refundChangeRule", "baggageInfo", "mealCode",
                      "reimbursementProof", "remainingSeats")
            info_key, options_key = "flight", "cabin_options"
            identity_keys, expected = ("flightNo", "flightNumber"), candidate["flight_no"]
        else:
            payload = next((p for p in payloads if isinstance(p, dict) and isinstance(p.get("data"), dict)), None)
            if payload is None:
                raise ValueError("Missing train detail")
            detail = payload["data"]
            info, options = detail.get("trainInfo", detail), detail.get("seatInfo", [])
            fields = ("leftNumber", "seatId", "seatName", "price", "resId", "seatStatus")
            info_key, options_key = "train", "seat_options"
            identity_keys, expected = ("trainNum", "trainNo"), candidate["train_no"]
        if info is not None and not isinstance(info, dict):
            raise ValueError("Invalid service detail")
        if not isinstance(options, list) or any(not isinstance(row, dict) for row in options):
            raise ValueError("Invalid detail options")
        if info and any(info.get(k) is not None and info[k] != expected for k in identity_keys):
            raise ValueError("Supplier returned another service")
        if info:
            for date_key in ("departureDate", "departsDate"):
                if info.get(date_key) is not None and info[date_key] != query["departure_date"]:
                    raise ValueError("Supplier returned another departure date")
            if mode == "train":
                for field, expected_station in (("departStationName", candidate["departure_station"]),
                                                ("destStationName", candidate["arrival_station"])):
                    if info.get(field) is not None and info[field] != expected_station:
                        raise ValueError("Supplier returned another station")
            departure = info.get("departureTime")
            if isinstance(departure, str) and len(departure) >= 10:
                parsed = datetime.fromisoformat(departure)
                departure_date = (parsed.replace(tzinfo=CHINA) if parsed.tzinfo is None else parsed).astimezone(CHINA).date()
                if departure_date.isoformat() != query["departure_date"]:
                    raise ValueError("Supplier returned another departure time")
        warnings = []
        if not info:
            warnings.append("供应商未返回班次详情；不从候选补造机场或航站楼。")
        return {"candidate_id": candidate["candidate_id"], "candidate_evidence_ref": candidate["evidence_ref"],
                "mode": mode, "departure_date": query["departure_date"], info_key: info,
                options_key: [{k: row[k] for k in fields if k in row} for row in options[:limit]],
                "returned_count": min(len(options), limit)}, warnings

    return await query_transport(mode, operation, arguments, args, "get_transport_detail", call_id,
                                 folder, timeout, transport_http, summarize)
