"""Executable runner for the refined seven-tool experiment."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from experiments.experiment_2_hil_tools.refined import HilTools
from experiments.external_tools.capture import Capture
from experiments.external_tools.config import load_settings

HERE = Path(__file__).resolve().parent
CHINA = timezone(timedelta(hours=8))


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def _first(result: dict, key: str) -> dict | None:
    data = result.get("data")
    rows = data.get(key, []) if isinstance(data, dict) else []
    return rows[0] if rows and isinstance(rows[0], dict) else None


async def run(args: argparse.Namespace) -> bool:
    day = args.date or (datetime.now(CHINA).date() + timedelta(days=3))
    run_id = datetime.now(CHINA).strftime("%Y%m%dT%H%M%S%f")
    folder = HERE / "results" / run_id
    evidence_folder = folder / "evidence"
    env_file = args.env_file or HERE.parent / "external_tools" / ".env"
    settings = load_settings(env_file)
    timeout = Decimal(str(args.timeout))
    reports: dict[str, dict] = {}

    async with httpx.AsyncClient(follow_redirects=False) as client:
        capture = Capture(settings, client, evidence_folder, timeout=float(timeout))
        tools = HilTools(capture, HERE / "policies" / "浙江省机关工作人员差旅费管理规定.md")
        reports["get_policy_snapshot"] = tools.get_policy_snapshot()
        reports["search_transport.flight"] = await tools.search_transport(mode="flight", origin=args.origin, destination=args.destination, departure_date=day, max_results=args.max_results)
        reports["search_transport.train"] = await tools.search_transport(mode="train", origin=args.origin, destination=args.destination, departure_date=day, service_class="high_speed_rail", max_results=args.max_results)
        reports["search_stays"] = await tools.search_stays(destination=args.destination, check_in=day, check_out=day + timedelta(days=args.nights), meeting_place=args.meeting, max_results=args.max_results)

        flight = _first(reports["search_transport.flight"], "candidates")
        train = _first(reports["search_transport.train"], "candidates")
        if flight and flight.get("flight_no"):
            reports["get_transport_detail"] = await tools.get_transport_detail(mode="flight", departure_date=day, origin=args.origin, destination=args.destination, flight_no=flight["flight_no"])
        elif train and train.get("train_no"):
            reports["get_transport_detail"] = await tools.get_transport_detail(mode="train", departure_date=day, train_no=train["train_no"], departure_station=train.get("departure_station"), arrival_station=train.get("arrival_station"))
        else:
            reports["get_transport_detail"] = {"tool": "get_transport_detail", "status": "skipped", "reason": "no transport candidate available"}

        hotel = _first(reports["search_stays"], "candidates")
        if hotel and hotel.get("hotel_id"):
            reports["get_stay_detail"] = await tools.get_stay_detail(hotel_id=hotel["hotel_id"], check_in=day, check_out=day + timedelta(days=args.nights))
        else:
            reports["get_stay_detail"] = {"tool": "get_stay_detail", "status": "skipped", "reason": "no hotel candidate available"}

        reports["resolve_place.meeting"] = await tools.resolve_place(city=args.destination, keyword=args.meeting, max_results=args.max_results)
        reports["resolve_place.hotel"] = await tools.resolve_place(city=args.destination, keyword=(hotel or {}).get("name") or args.hotel, max_results=args.max_results)
        meeting = _first(reports["resolve_place.meeting"], "candidates")
        hotel_place = _first(reports["resolve_place.hotel"], "candidates")
        origin_point = (hotel_place or {}).get("location")
        destination_point = (meeting or {}).get("location")
        if origin_point and destination_point:
            reports["calculate_route"] = await tools.calculate_route(origin=origin_point, destination=destination_point, origin_id=(hotel_place or {}).get("place_id"), destination_id=(meeting or {}).get("place_id"))
        else:
            reports["calculate_route"] = {"tool": "calculate_route", "status": "skipped", "reason": "place resolution did not return two coordinates"}

    for name, report in reports.items():
        _write(folder / f"{name.replace('.', '_')}.json", report)
        print(f"{name}: {report.get('status')}", flush=True)
    success = all(report.get("status") in {"ok", "skipped"} for report in reports.values())
    _write(folder / "summary.json", {"success": success, "run_id": run_id, "date": str(day), "reports": reports, "evidence_folder": str(evidence_folder)})
    print(f"Results: {folder}", flush=True)
    return success


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", type=date.fromisoformat)
    parser.add_argument("--origin", default="北京")
    parser.add_argument("--destination", default="上海")
    parser.add_argument("--meeting", default="上海国际会议中心")
    parser.add_argument("--hotel", default="上海国际会议中心")
    parser.add_argument("--nights", type=int, default=2)
    parser.add_argument("--max-results", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=45)
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(run(args)) else 1)


if __name__ == "__main__":
    main()
