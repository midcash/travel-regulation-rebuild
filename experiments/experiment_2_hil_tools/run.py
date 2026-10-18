"""Run the isolated seven-tool experiment with explicit travel constraints."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from experiments.experiment_2_hil_tools.capture import Capture
from experiments.experiment_2_hil_tools.config import load_settings
from experiments.experiment_2_hil_tools.tools import HilTools

HERE = Path(__file__).resolve().parent
CHINA = timezone(timedelta(hours=8))
MAX_RETRIES = 3


def _write(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def _first(result: dict[str, Any], key: str) -> dict[str, Any] | None:
    data = result.get("data")
    rows = data.get(key, []) if isinstance(data, dict) else []
    return rows[0] if rows and isinstance(rows[0], dict) else None


def _parse_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _filter_transport_by_arrival_date(
    result: dict[str, Any], allowed_arrival_date: date
) -> dict[str, Any]:
    """Keep only candidates that arrive on or before the intended stay date."""
    if result.get("status") != "ok":
        return result
    data = result.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("candidates"), list):
        return result

    filtered = []
    removed = 0
    for candidate in data["candidates"]:
        arrival = _parse_date(candidate.get("arrival_at")) if isinstance(candidate, dict) else None
        if arrival is not None and arrival <= allowed_arrival_date:
            filtered.append(candidate)
        else:
            removed += 1

    output = deepcopy(result)
    output_data = output["data"]
    output_data["candidates"] = filtered
    output_data["returned_count"] = len(filtered)
    output_data["filtered_out_count"] = removed
    if removed:
        output.setdefault("warnings", []).append(
            f"{removed} transport candidate(s) were removed because they arrive after {allowed_arrival_date}"
        )
    return output


async def _with_retries(
    name: str,
    operation: Callable[[], Awaitable[dict[str, Any]]],
    max_retries: int = MAX_RETRIES,
) -> tuple[dict[str, Any], int]:
    """Retry failed supplier calls; one initial call plus max_retries retries."""
    attempts = 0
    result: dict[str, Any] = {"tool": name, "status": "error", "data": None}
    while attempts <= max_retries:
        attempts += 1
        result = await operation()
        if result.get("status") in {"ok", "skipped"}:
            break
    return result, attempts


def _time(value: str) -> time:
    return time.fromisoformat(value)


async def run(args: argparse.Namespace) -> bool:
    meeting_date = args.meeting_date or (datetime.now(CHINA).date() + timedelta(days=3))
    departure_date = args.departure_date or meeting_date - timedelta(days=1)
    check_in = args.check_in or departure_date
    check_out = args.check_out or check_in + timedelta(days=args.nights)
    meeting_time = _time(args.meeting_time)
    arrival_deadline = args.arrival_deadline or (
        datetime.combine(meeting_date, meeting_time) - timedelta(minutes=args.arrival_buffer_minutes)
    )

    run_id = datetime.now(CHINA).strftime("%Y%m%dT%H%M%S%f")
    folder = HERE / "results" / run_id
    evidence_folder = folder / "evidence"
    env_file = args.env_file or HERE / ".env"
    settings = load_settings(env_file)
    timeout = Decimal(str(args.timeout))
    reports: dict[str, dict[str, Any]] = {}
    attempts: dict[str, int] = {}

    async with httpx.AsyncClient(follow_redirects=False) as client:
        capture = Capture(settings, client, evidence_folder, timeout=float(timeout))
        tools = HilTools(capture, HERE / "policies" / "浙江省机关工作人员差旅费管理规定.md")
        reports["get_policy_snapshot"] = tools.get_policy_snapshot()

        flight, attempts["search_transport.flight"] = await _with_retries(
            "search_transport",
            lambda: tools.search_transport(
                mode="flight", origin=args.origin, destination=args.destination,
                departure_date=departure_date, max_results=args.max_results,
            ),
            args.max_retries,
        )
        reports["search_transport.flight"] = _filter_transport_by_arrival_date(flight, check_in)

        train, attempts["search_transport.train"] = await _with_retries(
            "search_transport",
            lambda: tools.search_transport(
                mode="train", origin=args.origin, destination=args.destination,
                departure_date=departure_date, service_class="high_speed_rail",
                max_results=args.max_results,
            ),
            args.max_retries,
        )
        reports["search_transport.train"] = _filter_transport_by_arrival_date(train, check_in)

        reports["search_stays"], attempts["search_stays"] = await _with_retries(
            "search_stays",
            lambda: tools.search_stays(
                destination=args.destination, check_in=check_in, check_out=check_out,
                meeting_place=args.meeting, max_results=args.max_results,
            ),
            args.max_retries,
        )

        flight_candidate = _first(reports["search_transport.flight"], "candidates")
        if flight_candidate and flight_candidate.get("flight_no"):
            reports["get_transport_detail"], attempts["get_transport_detail"] = await _with_retries(
                "get_transport_detail",
                lambda: tools.get_transport_detail(
                    mode="flight", departure_date=departure_date, origin=args.origin,
                    destination=args.destination, flight_no=flight_candidate["flight_no"],
                ),
                args.max_retries,
            )
        else:
            reports["get_transport_detail"] = {
                "tool": "get_transport_detail", "status": "skipped",
                "reason": "no eligible flight candidate available",
            }

        hotel = _first(reports["search_stays"], "candidates")
        if hotel and hotel.get("hotel_id"):
            reports["get_stay_detail"], attempts["get_stay_detail"] = await _with_retries(
                "get_stay_detail",
                lambda: tools.get_stay_detail(
                    hotel_id=hotel["hotel_id"], check_in=check_in, check_out=check_out,
                ),
                args.max_retries,
            )
        else:
            reports["get_stay_detail"] = {
                "tool": "get_stay_detail", "status": "skipped",
                "reason": "no hotel candidate available",
            }

        reports["resolve_place.meeting"], attempts["resolve_place.meeting"] = await _with_retries(
            "resolve_place",
            lambda: tools.resolve_place(
                city=args.destination, keyword=args.meeting, max_results=args.max_results,
            ),
            args.max_retries,
        )
        reports["resolve_place.hotel"], attempts["resolve_place.hotel"] = await _with_retries(
            "resolve_place",
            lambda: tools.resolve_place(
                city=args.destination, keyword=(hotel or {}).get("name") or args.hotel,
                max_results=args.max_results,
            ),
            args.max_retries,
        )

        meeting = _first(reports["resolve_place.meeting"], "candidates")
        hotel_place = _first(reports["resolve_place.hotel"], "candidates")
        if meeting and hotel_place and meeting.get("location") and hotel_place.get("location"):
            reports["calculate_route"], attempts["calculate_route"] = await _with_retries(
                "calculate_route",
                lambda: tools.calculate_route(
                    origin=hotel_place["location"], destination=meeting["location"],
                    origin_id=hotel_place.get("place_id"), destination_id=meeting.get("place_id"),
                ),
                args.max_retries,
            )
        else:
            reports["calculate_route"] = {
                "tool": "calculate_route", "status": "skipped",
                "reason": "place resolution did not return two coordinates",
            }

    for name, report in reports.items():
        _write(folder / f"{name.replace('.', '_')}.json", report)
        print(f"{name}: {report.get('status')}", flush=True)

    context = {
        "meeting_date": meeting_date.isoformat(),
        "meeting_time": meeting_time.isoformat(),
        "arrival_deadline": arrival_deadline.isoformat(sep=" "),
        "departure_date": departure_date.isoformat(),
        "check_in": check_in.isoformat(),
        "check_out": check_out.isoformat(),
        "max_retries": args.max_retries,
    }
    success = all(report.get("status") in {"ok", "skipped"} for report in reports.values())
    _write(folder / "summary.json", {
        "success": success, "run_id": run_id, "context": context,
        "attempts": attempts, "reports": reports,
        "evidence_folder": str(evidence_folder),
    })
    print(f"Results: {folder}", flush=True)
    return success


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--meeting-date", "--date", dest="meeting_date", type=date.fromisoformat)
    parser.add_argument("--meeting-time", default="09:00")
    parser.add_argument("--arrival-deadline", type=datetime.fromisoformat)
    parser.add_argument("--arrival-buffer-minutes", type=int, default=30)
    parser.add_argument("--departure-date", type=date.fromisoformat)
    parser.add_argument("--check-in", type=date.fromisoformat)
    parser.add_argument("--check-out", type=date.fromisoformat)
    parser.add_argument("--origin", default="北京")
    parser.add_argument("--destination", default="上海")
    parser.add_argument("--meeting", default="上海国际会议中心")
    parser.add_argument("--hotel", default="上海国际会议中心")
    parser.add_argument("--nights", type=int, default=1)
    parser.add_argument("--max-results", type=int, default=5)
    parser.add_argument("--max-retries", type=int, default=MAX_RETRIES)
    parser.add_argument("--timeout", type=float, default=45)
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args()
    if args.max_retries < 0:
        parser.error("--max-retries must be non-negative")
    raise SystemExit(0 if asyncio.run(run(args)) else 1)


if __name__ == "__main__":
    main()
