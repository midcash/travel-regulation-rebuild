"""高德驾车路线适配器，迁移自 skill。"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from decimal import Decimal
from typing import cast

import httpx
from pydantic import ValidationError

from .config import Settings
from .models import ProviderQueryBase, RouteQuery, RouteResultItem, RouteProviderResult
from .values import GeoPoint
from .clock import Clock
from .errors import (
    ToolAuthenticationError,
    ToolBusinessError,
    ToolConfigurationError,
    ToolEmptyResultError,
    ToolRateLimitError,
    ToolResponseSchemaError,
    ToolTimeoutError,
    ToolTransportError,
)

MAX_RESPONSE_BYTES = 1_048_576
_AUTHENTICATION_INFOCODES = frozenset(
    {"10001", "10002", "10005", "10006", "10007", "10008", "10009", "10012", "10013"}
)
_RATE_LIMIT_INFOCODES = frozenset(
    {"10003", "10004", "10010", "10014", "10019", "10020"}
)


class AmapRouteProvider:
    """
    提供高德驾车路线

    """

    def __init__(
        self, settings: Settings, client: httpx.AsyncClient, clock: Clock
    ) -> None:
        """
        判断参数，完成初始化
        """
        if not isinstance(settings, Settings):
            raise TypeError("settings must be a Settings instance")
        if not isinstance(client, httpx.AsyncClient):
            raise TypeError("client must be an httpx.AsyncClient")
        if not isinstance(clock, Clock):
            raise TypeError("clock must implement Clock")
        if not settings.amap_api_key:
            raise ToolConfigurationError(
                provider="amap",
                operation="route_search",
                safe_message="provider credential is required for this adapter",
            )
        self._settings = settings
        self._client = client
        self._clock = clock
        self._api_key = settings.amap_api_key

    async def search(
        self, query: RouteQuery, *, timeout_seconds: Decimal
    ) -> RouteProviderResult:
        timeout = _timeout_as_float(timeout_seconds)
        params = {
            "key": self._api_key,
            "origin": _format_point(query.origin),
            "destination": _format_point(query.destination),
            "extensions": "base",
            "strategy": "32",
            "output": "JSON",
        }
        try:
            async with self._client.stream(
                "GET",
                self._settings.amap_driving_url,
                params=params,
                headers={"Accept": "application/json"},
                timeout=timeout,
            ) as response:
                _raise_for_http_status(
                    response.status_code, query, operation="route_search"
                )
                body = await _read_limited_body(response, query)
        except httpx.TimeoutException as exc:
            raise ToolTimeoutError(
                provider="amap",
                operation="route_search",
                trace_id=query.trace_id,
                query_id=query.query_id,
                safe_message="provider request timed out",
            ) from exc
        except httpx.TransportError as exc:
            raise ToolTransportError(
                provider="amap",
                operation="route_search",
                trace_id=query.trace_id,
                query_id=query.query_id,
                safe_message="provider transport failed",
            ) from exc

        payload = _decode_json_payload(body, query)
        _raise_for_amap_business_status(payload, query, operation="route_search")
        result = self._normalize_result(payload, query)
        return result

    def _normalize_result(
        self, payload: Mapping[str, object], query: RouteQuery
    ) -> RouteProviderResult:
        route = payload.get("route")
        if not isinstance(route, dict):
            raise _schema_error(query, "provider response has no route object")
        paths = route.get("paths")
        if not isinstance(paths, list):
            raise _schema_error(query, "provider response has no paths list")
        if not paths:
            raise ToolEmptyResultError(
                provider="amap",
                operation="route_search",
                trace_id=query.trace_id,
                query_id=query.query_id,
                safe_message="provider returned no usable routes",
            )
        try:
            items = tuple(
                _normalize_route_item(item, index, self._settings.amap_driving_url)
                for index, item in enumerate(paths)
            )
            return RouteProviderResult(
                query_id=query.query_id,
                provider="amap",
                observed_at=self._clock.now(),
                source_ref=self._settings.amap_driving_url,
                items=items,
            )
        except (TypeError, ValueError, ValidationError) as exc:
            raise _schema_error(
                query, "provider response does not match route schema"
            ) from exc


def _timeout_as_float(timeout_seconds: Decimal) -> float:
    """将显式超时预算安全地传递给 httpx。"""
    if isinstance(timeout_seconds, bool):
        raise TypeError("timeout_seconds must be a Decimal-compatible number")
    timeout = float(timeout_seconds)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout_seconds must be positive and finite")
    return timeout


async def _read_limited_body(
    response: httpx.Response, query: ProviderQueryBase
) -> bytes:
    """读取受大小限制的响应，避免原始 payload 无界进入进程。"""
    chunks: list[bytes] = []
    total_size = 0
    async for chunk in response.aiter_bytes():
        total_size += len(chunk)
        if total_size > MAX_RESPONSE_BYTES:
            raise _schema_error(query, "provider response exceeds the size limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _raise_for_http_status(
    status_code: int,
    query: ProviderQueryBase,
    *,
    operation: str = "route_search",
) -> None:
    """将 HTTP 状态映射到稳定且无敏感信息的 ToolError。"""
    error_kwargs = _error_kwargs(query, operation=operation)
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


def _decode_json_payload(body: bytes, query: ProviderQueryBase) -> Mapping[str, object]:
    """解码 JSON object，拒绝非对象和非 UTF-8 响应。"""
    try:
        value: object = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _schema_error(query, "provider response is not valid JSON") from exc
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise _schema_error(query, "provider response must be a JSON object")
    return cast(dict[str, object], value)


def _raise_for_amap_business_status(
    payload: Mapping[str, object],
    query: ProviderQueryBase,
    *,
    operation: str = "route_search",
) -> None:
    """处理高德在 HTTP 200 内返回的业务失败。"""
    status = payload.get("status")
    if not isinstance(status, str):
        raise _schema_error(query, "provider response has an invalid status field")
    if status == "1":
        return
    if status != "0":
        raise _schema_error(query, "provider response has an unsupported status value")

    infocode = payload.get("infocode")
    if not isinstance(infocode, str):
        raise _schema_error(query, "provider error response has no infocode")
    error_kwargs = _error_kwargs(query, operation=operation)
    if infocode in _AUTHENTICATION_INFOCODES:
        raise ToolAuthenticationError(
            **error_kwargs,
            safe_message="provider rejected the request credentials",
        )
    if infocode in _RATE_LIMIT_INFOCODES:
        raise ToolRateLimitError(
            **error_kwargs,
            safe_message="provider rate limit rejected the request",
        )
    raise ToolBusinessError(
        **error_kwargs,
        safe_message="provider reported a business failure",
    )


def _normalize_route_item(
    value: object, index: int, source_ref: str
) -> RouteResultItem:
    """清理并转换一个高德 paths item。"""
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise TypeError("route path must be an object")
    return RouteResultItem(
        entity_id=f"amap.route:{index + 1}",
        mode="driving",
        duration_seconds=_positive_integer(value.get("duration"), "duration"),
        distance_meters=_positive_integer(value.get("distance"), "distance"),
        source_ref=source_ref,
    )


def _positive_integer(value: object, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"route {field_name} must be an integer")
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"route {field_name} must be an integer") from exc
    if parsed <= 0:
        raise ValueError(f"route {field_name} must be positive")
    return parsed


def _format_point(point: GeoPoint) -> str:
    return f"{point.longitude:.6f},{point.latitude:.6f}"


def _schema_error(
    query: ProviderQueryBase, safe_message: str
) -> ToolResponseSchemaError:
    """构造附带查询关联信息的响应 Schema 错误。"""
    return ToolResponseSchemaError(**_error_kwargs(query), safe_message=safe_message)


def _error_kwargs(
    query: ProviderQueryBase, *, operation: str = "route_search"
) -> dict[str, str]:
    """返回不含用户文本和凭证的 ToolError 上下文。"""
    return {
        "provider": "amap",
        "operation": operation,
        "trace_id": query.trace_id,
        "query_id": query.query_id,
    }
