"""Tuniu legacy API-key MCP adapter for M3 travel-search ports."""

from __future__ import annotations

import hashlib
import html
import json
import math
import re
from collections.abc import Mapping
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import cast

import httpx
from pydantic import ValidationError

from .config import (
    DEFAULT_TUNIU_FLIGHT_URL,
    DEFAULT_TUNIU_HOTEL_URL,
    DEFAULT_TUNIU_TRAIN_URL,
    Settings,
)
from .models import (
    ProviderQueryBase,
    StayProviderResult,
    StayQuery,
    StayResultItem,
    TransportProviderResult,
    TransportQuery,
    TransportResultItem,
)
from .values import Money
from .clock import Clock
from .errors import (
    ToolAuthenticationError,
    ToolBusinessError,
    ToolConfigurationError,
    ToolConstraintNoMatchError,
    ToolEmptyResultError,
    ToolRateLimitError,
    ToolResponseSchemaError,
    ToolTimeoutError,
    ToolTransportError,
)

# Backward-compatible public aliases; runtime calls use the Settings snapshot.
TUNIU_HOTEL_URL = DEFAULT_TUNIU_HOTEL_URL
TUNIU_FLIGHT_URL = DEFAULT_TUNIU_FLIGHT_URL
TUNIU_TRAIN_URL = DEFAULT_TUNIU_TRAIN_URL
MAX_RESPONSE_BYTES = 1_048_576
_JSON_RPC_REQUEST_ID = 1

_CHINA_TIMEZONE = timezone(timedelta(hours=8))
_CONTROL_CHARS = re.compile(r"[\x00-\x1F\x7F]")
_HTML_TAGS = re.compile(r"<[^>]*>")


class TuniuTravelProvider:
    def __init__(
        self, settings: Settings, client: httpx.AsyncClient, clock: Clock
    ) -> None:
        """Keep the immutable composition-root dependencies for one request."""
        if not isinstance(settings, Settings):
            raise TypeError("settings must be a Settings instance")
        if not isinstance(client, httpx.AsyncClient):
            raise TypeError("client must be an httpx.AsyncClient")
        if not isinstance(clock, Clock):
            raise TypeError("clock must implement Clock")
        if not settings.tuniu_api_key:
            raise ToolConfigurationError(
                provider="tuniu",
                operation="travel_search",
                safe_message="provider credential is required for this adapter",
            )

        self._settings = settings
        self._client = client
        self._clock = clock
        self._api_key = settings.tuniu_api_key

    async def _search_transport(
        self, query: TransportQuery, *, timeout_seconds: Decimal
    ) -> TransportProviderResult:
        """Search exactly one explicitly selected domestic transport tool."""
        if query.departure_after is None:
            raise ToolBusinessError(
                **_error_kwargs(query, "transport_search"),
                safe_message="provider requires a departure date",
            )
        is_train = query.mode == "train"
        endpoint = (
            self._settings.tuniu_train_url
            if is_train
            else self._settings.tuniu_flight_url
        )
        tool_name = "searchLowestPriceTrain" if is_train else "searchLowestPriceFlight"
        payload = await self._call_tool(
            endpoint=endpoint,
            tool_name=tool_name,
            operation="transport_search",
            query=query,
            timeout_seconds=timeout_seconds,
            arguments={
                "departureCityName": query.origin,
                "arrivalCityName": query.destination,
                "departureDate": query.departure_after.astimezone(_CHINA_TIMEZONE)
                .date()
                .isoformat(),
            },
        )
        _require_success(payload, "successCode", query, "transport_search")
        records = _required_list(payload, "data", query, "transport_search")
        if not records:
            raise _empty_error(query, "transport_search")

        try:
            items = tuple(
                (
                    _normalize_train_item(record, query, endpoint)
                    if is_train
                    else _normalize_flight_item(record, query, endpoint)
                )
                for record in records
            )
            if is_train and query.service_class == "high_speed_rail":
                items = tuple(
                    item for item in items if item.service_class == "high_speed_rail"
                )
                if not items:
                    raise _constraint_no_match_error(query, "transport_search")
            items = items[: query.max_results]
            if not items:
                raise _empty_error(query, "transport_search")
            return TransportProviderResult(
                query_id=query.query_id,
                provider="tuniu",
                observed_at=self._clock.now(),
                source_ref=endpoint,
                items=items,
            )
        except (TypeError, ValueError, ValidationError, InvalidOperation) as exc:
            raise _schema_error(
                query,
                "transport_search",
                "response does not match transport schema",
            ) from exc

    async def _search_stay(
        self, query: StayQuery, *, timeout_seconds: Decimal
    ) -> StayProviderResult:
        """Search the documented hotel tool once."""
        arguments: dict[str, object] = {
            "cityName": query.destination,
            "checkIn": query.date_range.start.isoformat(),
            "checkOut": query.date_range.end.isoformat(),
            "adultNum": query.travelers,
        }
        if query.area is not None:
            arguments["keyword"] = query.area
        if query.max_total_price is not None:
            arguments["prices"] = f"0-{query.max_total_price.amount}"

        payload = await self._call_tool(
            endpoint=self._settings.tuniu_hotel_url,
            tool_name="tuniuHotelSearch",
            operation="stay_search",
            query=query,
            timeout_seconds=timeout_seconds,
            arguments=arguments,
        )
        _require_success(payload, "success", query, "stay_search")
        records = _required_list(payload, "hotels", query, "stay_search")
        if not records:
            raise _empty_error(query, "stay_search")

        try:
            items = tuple(
                _normalize_hotel_item(record, query, self._settings.tuniu_hotel_url)
                for record in records[: query.max_results]
            )
            return StayProviderResult(
                query_id=query.query_id,
                provider="tuniu",
                observed_at=self._clock.now(),
                source_ref=self._settings.tuniu_hotel_url,
                items=items,
            )
        except (TypeError, ValueError, ValidationError, InvalidOperation) as exc:
            raise _schema_error(
                query,
                "stay_search",
                "response does not match hotel schema",
            ) from exc

    async def _call_tool(
        self,
        *,
        endpoint: str,
        tool_name: str,
        operation: str,
        query: ProviderQueryBase,
        timeout_seconds: Decimal,
        arguments: Mapping[str, object],
    ) -> Mapping[str, object]:
        """Issue exactly one legacy API-key JSON-RPC request and unwrap its result."""
        request_payload = {
            "jsonrpc": "2.0",
            "id": _JSON_RPC_REQUEST_ID,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": dict(arguments)},
        }
        try:
            async with self._client.stream(
                "POST",
                endpoint,
                json=request_payload,
                headers={
                    "Accept": "application/json, text/event-stream",
                    "apiKey": self._api_key,
                },
                timeout=_timeout_as_float(timeout_seconds),
            ) as response:
                _raise_for_http_status(response.status_code, query, operation)
                body = await _read_limited_body(response, query, operation)
        except httpx.TimeoutException as exc:
            raise ToolTimeoutError(
                **_error_kwargs(query, operation),
                safe_message="provider request timed out",
            ) from exc
        except httpx.TransportError as exc:
            raise ToolTransportError(
                **_error_kwargs(query, operation),
                safe_message="provider transport failed",
            ) from exc
        except OSError as exc:
            raise ToolTransportError(
                **_error_kwargs(query, operation),
                safe_message="provider transport failed",
            ) from exc
        return _decode_mcp_result(body, query, operation)

    async def search(
        self, query: TransportQuery | StayQuery, *, timeout_seconds: Decimal
    ) -> TransportProviderResult | StayProviderResult:
        """单次查询，不重试、不缓存；交通和酒店共享途牛客户端。"""
        if isinstance(query, TransportQuery):
            return await self._search_transport(query, timeout_seconds=timeout_seconds)
        if isinstance(query, StayQuery):
            return await self._search_stay(query, timeout_seconds=timeout_seconds)
        raise TypeError("query must be TransportQuery or StayQuery")


def _timeout_as_float(timeout_seconds: Decimal) -> float:
    """Convert the explicit Decimal timeout budget for httpx."""
    if isinstance(timeout_seconds, bool):
        raise TypeError("timeout_seconds must be a Decimal-compatible number")
    timeout = float(timeout_seconds)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout_seconds must be positive and finite")
    return timeout


async def _read_limited_body(
    response: httpx.Response,
    query: ProviderQueryBase,
    operation: str,
) -> bytes:
    """Read one bounded JSON response or the first complete SSE event."""
    chunks: list[bytes] = []
    total_size = 0
    is_sse = "text/event-stream" in response.headers.get("content-type", "").lower()
    try:
        async for chunk in response.aiter_bytes():
            total_size += len(chunk)
            if total_size > MAX_RESPONSE_BYTES:
                raise _schema_error(
                    query, operation, "provider response exceeds the size limit"
                )
            chunks.append(chunk)
            if is_sse:
                event = _first_sse_event(b"".join(chunks))
                if event is not None:
                    return event
    except httpx.RemoteProtocolError as exc:
        body = b"".join(chunks)
        if is_sse and _has_complete_sse_json_rpc(body):
            return body
        if not body:
            safe_message = "provider disconnected before returning a response body"
        elif is_sse:
            safe_message = "provider returned an incomplete SSE response"
        else:
            safe_message = "provider returned an incomplete non-SSE response"
        raise ToolTransportError(
            **_error_kwargs(query, operation),
            safe_message=safe_message,
        ) from exc
    return b"".join(chunks)


def _raise_for_http_status(
    status_code: int,
    query: ProviderQueryBase,
    operation: str,
) -> None:
    """Map HTTP failures to safe, typed M3 errors."""
    error_kwargs = _error_kwargs(query, operation)
    if status_code in {401, 403}:
        raise ToolAuthenticationError(
            **error_kwargs,
            safe_message="provider rejected the request credentials",
        )
    if status_code == 429:
        raise ToolRateLimitError(
            **error_kwargs,
            safe_message="provider rate limit rejected the request",
        )
    if 500 <= status_code <= 599:
        raise ToolTransportError(
            **error_kwargs,
            safe_message="provider service is unavailable",
        )
    if 400 <= status_code <= 499:
        raise ToolBusinessError(
            **error_kwargs,
            safe_message="provider rejected the request",
        )
    if 300 <= status_code <= 399:
        raise ToolTransportError(
            **error_kwargs,
            safe_message="provider returned an unsupported redirect response",
        )


def _first_sse_event(body: bytes) -> bytes | None:
    """Return the first complete SSE event without waiting for stream closure."""
    for separator in (b"\r\n\r\n", b"\n\n"):
        boundary = body.find(separator)
        if boundary >= 0:
            return body[: boundary + len(separator)]
    return None


def _has_complete_sse_json_rpc(body: bytes) -> bool:
    """Accept one complete JSON-RPC SSE payload despite a broken chunk terminator."""
    try:
        decoded = body.decode("utf-8")
    except UnicodeDecodeError:
        return False
    data_lines = tuple(
        line.removeprefix("data:").strip()
        for line in decoded.splitlines()
        if line.startswith("data:")
        and line.removeprefix("data:").strip() not in {"", "[DONE]"}
    )
    if len(data_lines) != 1:
        return False
    try:
        envelope = json.loads(data_lines[0])
    except json.JSONDecodeError:
        return False
    return (
        isinstance(envelope, dict)
        and envelope.get("jsonrpc") == "2.0"
        and "id" in envelope
        and ("result" in envelope or "error" in envelope)
    )


def _decode_mcp_result(
    body: bytes,
    query: ProviderQueryBase,
    operation: str,
) -> Mapping[str, object]:
    """Decode JSON or one SSE data event and validate the JSON-RPC envelope."""
    envelope = _decode_json_or_sse(body, query, operation)
    if envelope.get("jsonrpc") != "2.0":
        raise _schema_error(
            query, operation, "provider response has an invalid JSON-RPC version"
        )
    if "error" in envelope:
        _raise_for_mcp_error(envelope["error"], query, operation)
    if "result" not in envelope:
        raise _schema_error(
            query, operation, "provider response has no JSON-RPC result"
        )
    return _unwrap_mcp_result(envelope["result"], query, operation)


def _decode_json_or_sse(
    body: bytes,
    query: ProviderQueryBase,
    operation: str,
) -> Mapping[str, object]:
    """Decode either a JSON object or exactly one server-sent JSON event."""
    try:
        decoded = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _schema_error(query, operation, "provider response is not UTF-8") from exc

    if "data:" in decoded:
        events: list[Mapping[str, object]] = []
        for line in decoded.splitlines():
            if not line.startswith("data:"):
                continue
            event_data = line.removeprefix("data:").strip()
            if not event_data or event_data == "[DONE]":
                continue
            events.append(_decode_json_object(event_data, query, operation))
        if len(events) != 1:
            raise _schema_error(
                query, operation, "provider SSE response is not a single result"
            )
        return events[0]
    return _decode_json_object(decoded, query, operation)


def _decode_json_object(
    value: str,
    query: ProviderQueryBase,
    operation: str,
) -> Mapping[str, object]:
    """Decode a provider JSON object without retaining raw payload data."""
    try:
        parsed: object = json.loads(value)
    except json.JSONDecodeError as exc:
        raise _schema_error(
            query, operation, "provider response is not valid JSON"
        ) from exc
    if not isinstance(parsed, dict) or any(not isinstance(key, str) for key in parsed):
        raise _schema_error(query, operation, "provider response must be a JSON object")
    return cast(dict[str, object], parsed)


def _raise_for_mcp_error(
    value: object,
    query: ProviderQueryBase,
    operation: str,
) -> None:
    """Translate a JSON-RPC error object without exposing its message."""
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise _schema_error(query, operation, "provider JSON-RPC error is malformed")
    error = cast(dict[str, object], value)
    code = error.get("code")
    error_kwargs = _error_kwargs(query, operation)
    if code in {401, 403, "401", "403", "UNAUTHORIZED", "FORBIDDEN"}:
        raise ToolAuthenticationError(
            **error_kwargs,
            safe_message="provider rejected the request credentials",
        )
    if code in {429, "429", "RATE_LIMITED"}:
        raise ToolRateLimitError(
            **error_kwargs,
            safe_message="provider rate limit rejected the request",
        )
    raise ToolBusinessError(
        **error_kwargs,
        safe_message="provider reported a business failure",
    )


def _unwrap_mcp_result(
    value: object,
    query: ProviderQueryBase,
    operation: str,
) -> Mapping[str, object]:
    """Extract the typed tool payload from a direct or text-content result."""
    result = _as_mapping(value, query, operation, "provider JSON-RPC result")
    if result.get("isError") is True:
        raise ToolBusinessError(
            **_error_kwargs(query, operation),
            safe_message="provider reported a tool failure",
        )
    if "content" not in result:
        return result

    content = _required_list(result, "content", query, operation)
    if len(content) != 1:
        raise _schema_error(
            query, operation, "provider content must contain one text block"
        )
    block = _as_mapping(content[0], query, operation, "provider content block")
    if block.get("type") != "text" or not isinstance(block.get("text"), str):
        raise _schema_error(
            query, operation, "provider content must be one JSON text block"
        )
    return _decode_json_object(cast(str, block["text"]), query, operation)


def _require_success(
    payload: Mapping[str, object],
    field_name: str,
    query: ProviderQueryBase,
    operation: str,
) -> None:
    """Require a documented provider success flag before normalization."""
    success = payload.get(field_name)
    if success is True:
        return
    if success is False:
        raise ToolBusinessError(
            **_error_kwargs(query, operation),
            safe_message="provider reported a business failure",
        )
    raise _schema_error(
        query, operation, "provider response has an invalid success field"
    )


def _required_list(
    payload: Mapping[str, object],
    field_name: str,
    query: ProviderQueryBase,
    operation: str,
) -> list[object]:
    """Return one required provider list or raise a response-schema error."""
    value = payload.get(field_name)
    if not isinstance(value, list):
        raise _schema_error(
            query, operation, f"provider response has no {field_name} list"
        )
    return value


def _normalize_flight_item(
    value: object, query: TransportQuery, source_ref: str
) -> TransportResultItem:
    """Normalize one documented flight record to the transport DTO."""
    item = _plain_mapping(value)
    flight_number = _clean_external_text(item.get("flightNumber"))
    airline = _clean_external_text(item.get("airlineCompany"))
    departure_at = _parse_tuniu_datetime(item.get("departureTime"))
    arrival_at = _parse_tuniu_datetime(item.get("arrivalTime"))
    total_price = _flight_total_price(item)
    return TransportResultItem(
        entity_id=_stable_id("flight", flight_number, item.get("departureTime")),
        mode="flight",
        name=f"{airline} {flight_number}",
        origin=query.origin,
        destination=query.destination,
        departure_at=departure_at,
        arrival_at=arrival_at,
        total_price=total_price,
        source_ref=source_ref,
    )


def _normalize_train_item(
    value: object,
    query: TransportQuery,
    source_ref: str,
) -> TransportResultItem:
    """Normalize one documented train record to the transport DTO."""
    item = _plain_mapping(value)
    train_number = _clean_external_text(item.get("trainNum"))
    departure_station = _clean_external_text(item.get("departStationName"))
    arrival_station = _clean_external_text(item.get("destStationName"))
    departure_at = _parse_tuniu_datetime(item.get("departureTime"))
    arrival_at = _parse_tuniu_datetime(item.get("arrivalTime"))
    train_type = _clean_external_text(item.get("trainType"))
    return TransportResultItem(
        entity_id=_stable_id("train", train_number, item.get("departureTime")),
        mode="train",
        service_class=_train_service_class(train_number),
        name=f"{train_type} {train_number}",
        origin=query.origin,
        destination=query.destination,
        departure_station=departure_station,
        arrival_station=arrival_station,
        departure_at=departure_at,
        arrival_at=arrival_at,
        total_price=_train_total_price(item),
        source_ref=source_ref,
    )


def _normalize_hotel_item(
    value: object, query: StayQuery, source_ref: str
) -> StayResultItem:
    """Normalize one hotel record and preserve the requested stay window."""
    item = _plain_mapping(value)
    hotel_id = item.get("hotelId")
    if isinstance(hotel_id, bool) or not isinstance(hotel_id, str | int):
        raise TypeError("hotelId must be a string or integer")
    return StayResultItem(
        entity_id=_stable_id("hotel", hotel_id),
        name=_clean_external_text(item.get("hotelName")),
        area=_clean_external_text(item.get("cityName")),
        check_in=datetime.combine(
            query.date_range.start, time.min, tzinfo=_CHINA_TIMEZONE
        ),
        check_out=datetime.combine(
            query.date_range.end, time.min, tzinfo=_CHINA_TIMEZONE
        ),
        total_price=_money(item.get("lowestPrice")),
        source_ref=source_ref,
    )


def _flight_total_price(item: Mapping[str, object]) -> Money:
    """Add the documented base fare and taxes without binary floating-point math."""
    base_price = _decimal(item.get("basePrice"))
    total_tax = _decimal(item.get("totalTax"))
    return Money(amount=base_price + total_tax, currency="CNY")


def _train_service_class(train_number: str) -> str:
    """按受控车次前缀区分高铁类别与普通铁路。"""
    prefix = train_number[:1].upper()
    return "high_speed_rail" if prefix in {"G", "D", "C"} else "standard_rail"


def _train_total_price(item: Mapping[str, object]) -> Money:
    """Select the lowest positive seat quote from one train result."""
    prices = _plain_mapping(item.get("price"))
    available: list[Decimal] = []
    for field_name in (
        "edzPrice",
        "ydzPrice",
        "yzPrice",
        "wzPrice",
        "rzPrice",
        "rwPrice",
        "ywPrice",
        "swzPrice",
        "tdzPrice",
        "dwPrice",
        "ydwPrice",
        "edwPrice",
        "gjrwPrice",
    ):
        value = prices.get(field_name)
        if value in {None, ""}:
            continue
        amount = _decimal(value)
        if amount > 0:
            available.append(amount)
    if not available:
        raise ValueError("provider train result has no usable seat price")
    return Money(amount=min(available), currency="CNY")


def _money(value: object) -> Money:
    """Convert a documented CNY quote to the shared Money value object."""
    return Money(amount=_decimal(value), currency="CNY")


def _decimal(value: object) -> Decimal:
    """Parse an exact provider number and reject floats and booleans."""
    if isinstance(value, bool) or not isinstance(value, str | int | Decimal):
        raise TypeError("provider price must be an integer, decimal string, or Decimal")
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("provider price must be a valid decimal") from exc


def _parse_tuniu_datetime(value: object) -> datetime:
    """Interpret documented domestic-flight local timestamps in China Standard Time."""
    text = _clean_external_text(value)
    for format_string in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, format_string).replace(
                tzinfo=_CHINA_TIMEZONE
            )
        except ValueError:
            continue
    raise ValueError("provider flight time has an unsupported format")


def _clean_external_text(value: object) -> str:
    """Strip HTML and control characters from untrusted provider text."""
    if not isinstance(value, str):
        raise TypeError("provider text field must be a string")
    cleaned = _CONTROL_CHARS.sub("", _HTML_TAGS.sub("", html.unescape(value))).strip()
    if not cleaned:
        raise ValueError("provider text field must not be empty")
    return cleaned


def _as_mapping(
    value: object,
    query: ProviderQueryBase,
    operation: str,
    description: str,
) -> Mapping[str, object]:
    """Validate an external JSON object while retaining no raw payload."""
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise _schema_error(query, operation, f"{description} must be an object")
    return cast(dict[str, object], value)


def _plain_mapping(value: object) -> Mapping[str, object]:
    """Validate one collection item before its field-level normalization."""
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise TypeError("provider collection item must be an object")
    return cast(dict[str, object], value)


def _stable_id(kind: str, *parts: object) -> str:
    """Create a stable provider entity identifier without query data or credentials."""
    seed = "\x1f".join(str(part) for part in parts)
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]
    return f"tuniu.{kind}:{digest}"


def _empty_error(query: ProviderQueryBase, operation: str) -> ToolEmptyResultError:
    """Create a typed empty-result error with only safe query identifiers."""
    return ToolEmptyResultError(
        **_error_kwargs(query, operation),
        safe_message="provider returned no usable results",
    )


def _constraint_no_match_error(
    query: ProviderQueryBase, operation: str
) -> ToolConstraintNoMatchError:
    """Create a safe error for results rejected by a hard query constraint."""
    return ToolConstraintNoMatchError(
        **_error_kwargs(query, operation),
        safe_message="provider results do not match the requested transport class",
    )


def _schema_error(
    query: ProviderQueryBase,
    operation: str,
    safe_message: str,
) -> ToolResponseSchemaError:
    """Create a typed response-schema error with only safe identifiers."""
    return ToolResponseSchemaError(
        **_error_kwargs(query, operation),
        safe_message=safe_message,
    )


def _error_kwargs(query: ProviderQueryBase, operation: str) -> dict[str, str]:
    """Return non-sensitive ToolError context for one Tuniu request."""
    return {
        "provider": "tuniu",
        "operation": operation,
        "trace_id": query.trace_id,
        "query_id": query.query_id,
    }
