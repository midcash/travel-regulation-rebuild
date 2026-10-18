"""Transport DTO mapping migrated from the current experiment-two tool."""
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
        return parsed if parsed.is_finite() and parsed >= 0 else None
    except (InvalidOperation, ValueError):
        return None


def _money(value: Any) -> str | None:
    parsed = _decimal(value)
    return format(parsed, "f") if parsed is not None else None


def _is_high_speed(row: Mapping[str, Any]) -> bool:
    train_no = str(row.get("trainNum", "")).upper()
    return train_no.startswith(("G", "D", "C"))


def _flight_summary(row: Mapping[str, Any], evidence: str) -> dict[str, Any]:
    base = _decimal(row.get("basePrice"))
    tax = _decimal(row.get("totalTax"))
    total = base + tax if base is not None and tax is not None else _decimal(row.get("totalPrice"))
    return {
        "candidate_id": f"flight:{row.get('flightNumber', 'unknown')}",
        "mode": "flight",
        "flight_no": _text(row.get("flightNumber")),
        "operator": _text(row.get("airlineCompany")),
        "departure_at": _text(row.get("departureTime")),
        "arrival_at": _text(row.get("arrivalTime")),
        "departure_airport": _text(row.get("departureAirport")),
        "arrival_airport": _text(row.get("arrivalAirport")),
        "price_cny": format(total, "f") if total is not None else None,
        "base_price_cny": _money(row.get("basePrice")),
        "tax_cny": _money(row.get("totalTax")),
        "remaining_seats": row.get("remainingSeats"),
        "evidence_ref": evidence,
    }


def _train_summary(row: Mapping[str, Any], evidence: str) -> dict[str, Any]:
    raw_prices = row.get("price")
    prices = raw_prices if isinstance(raw_prices, Mapping) else {}
    numeric = [parsed for value in prices.values() if (parsed := _decimal(value)) is not None and parsed > 0]
    lowest = min(numeric) if numeric else None
    return {
        "candidate_id": f"train:{row.get('trainNum', 'unknown')}",
        "mode": "train",
        "train_no": _text(row.get("trainNum")),
        "train_type": _text(row.get("trainType")),
        "departure_station": _text(row.get("departStationName")),
        "arrival_station": _text(row.get("destStationName")),
        "departure_at": _text(row.get("departureTime")),
        "arrival_at": _text(row.get("arrivalTime")),
        "duration": _text(row.get("duration")),
        "lowest_price_cny": format(lowest, "f") if lowest is not None else None,
        "seat_prices": dict(prices),
        "evidence_ref": evidence,
    }


