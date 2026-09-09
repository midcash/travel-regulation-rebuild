"""实验专用：完整解码供应商响应，不经过业务 DTO，不选择或重命名字段。"""

from __future__ import annotations

import asyncio
import json
import math
import re
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from time import perf_counter
from uuid import uuid4

import httpx

from .config import Settings

MAX_BYTES = 16 * 1024 * 1024
READ_TOOLS = {
    "flight": {
        "searchLowestPriceFlight",
        "multiCabinDetails",
        "getBookingRequiredInfo",
    },
    "train": {"searchLowestPriceTrain", "queryTrainDetail"},
    "hotel": {"tuniuHotelSearch", "tuniuHotelDetail"},
}
AMAP_PATHS = {
    "geocode": "/v3/geocode/geo",
    "reverse_geocode": "/v3/geocode/regeo",
    "poi_search": "/v5/place/text",
    "poi_detail": "/v5/place/detail",
    "poi_around": "/v5/place/around",
    "driving_v3": "/v3/direction/driving",
    "driving_v5": "/v5/direction/driving",
}


def loads(text: str):
    def reject_constant(value):
        raise ValueError(f"Non-JSON numeric constant: {value}")

    return json.loads(text, parse_float=Decimal, parse_constant=reject_constant)


def dumps(value) -> str:
    # 小数以十进制字符串落盘，field_inventory 保留原始 decimal 类型；整数不转换。
    return json.dumps(
        value, ensure_ascii=False, indent=2, default=decimal_json, allow_nan=False
    )


def decimal_json(value):
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"Unsupported JSON value: {type(value).__name__}")


def decode_wire(body: bytes, is_sse: bool) -> list:
    text = body.decode("utf-8-sig")
    if not is_sse:
        return [loads(text)]
    events = []
    for event in re.split(r"\r?\n\r?\n", text):
        lines = [
            line[5:].lstrip(" ")
            for line in event.splitlines()
            if line.startswith("data:")
        ]
        data = "\n".join(lines)
        if data and data != "[DONE]":
            events.append(loads(data))
    return events


def complete_sse(body: bytes) -> tuple[list, bytes]:
    """按事件分隔符取完整前缀；不要求网络 chunk 恰好停在事件末尾。"""
    boundaries = list(re.finditer(rb"\r?\n\r?\n", body))
    end = boundaries[-1].end() if boundaries else 0
    return decode_wire(body[:end], True), body[end:]


def redact(value, secrets):
    if isinstance(value, dict):
        return {redact(k, secrets): redact(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    if isinstance(value, str):
        # wire_text/content.text 中可能把密钥写成 Unicode 转义，甚至多重 JSON 转义。
        unescaped = re.sub(r"\\+u([0-9a-fA-F]{4})", lambda m: chr(int(m[1], 16)), value)
        for secret in secrets:
            if secret and secret in unescaped and unescaped != value:
                return "[REDACTED_TEXT_CONTAINING_API_KEY]"
            if secret:
                value = value.replace(secret, "[REDACTED_API_KEY]")
        return value
    return value


def unpack(envelope: dict) -> list:
    """保留所有 content block；JSON 文本展开，普通文本不强行当成 JSON。"""
    result = envelope.get("result", envelope)
    if not isinstance(result, dict):
        return [result]
    payloads = []
    for block in result.get("content", []):
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            try:
                payloads.append(loads(text))
            except (ValueError, TypeError):
                if isinstance(text, str) and text.lstrip().startswith(("{", "[")):
                    raise ValueError("Malformed JSON content block")
                payloads.append({"text": text})
        else:
            payloads.append(block)
    if "structuredContent" in result:
        structured = result["structuredContent"]
        # 当前途牛 structuredContent.result 是另一份 JSON 字符串；保留外层并展开。
        if isinstance(structured, dict) and isinstance(structured.get("result"), str):
            try:
                payloads.append(loads(structured["result"]))
            except ValueError:
                if structured["result"].lstrip().startswith(("{", "[")):
                    raise ValueError("Malformed JSON structuredContent.result")
        payloads.append(structured)
    return payloads or [result]


def field_inventory(value, prefix="$") -> dict:
    """合并数组元素的字段路径，区分缺字段、null、空字符串和空容器。"""
    result = {}

    def visit(item, path):
        kind = (
            "null"
            if item is None
            else "boolean"
            if isinstance(item, bool)
            else "decimal"
            if isinstance(item, Decimal)
            else "integer"
            if isinstance(item, int)
            else "object"
            if isinstance(item, dict)
            else "array"
            if isinstance(item, list)
            else "string"
            if isinstance(item, str)
            else type(item).__name__
        )
        stat = result.setdefault(
            path, {"types": [], "occurrences": 0, "null": 0, "empty": 0}
        )
        if kind not in stat["types"]:
            stat["types"].append(kind)
        stat["occurrences"] += 1
        stat["null"] += item is None
        stat["empty"] += item == "" or item == [] or item == {}
        if isinstance(item, dict):
            for key, child in item.items():
                visit(child, f"{path}.{key}")
        elif isinstance(item, list):
            for child in item:
                visit(child, f"{path}[]")

    visit(value, prefix)
    return result


def check_expected(payloads: list, expected: list[str]) -> dict:
    inventory = {}
    for payload in payloads:
        for path, stat in field_inventory(payload).items():
            target = inventory.setdefault(
                path, {"types": [], "occurrences": 0, "null": 0, "empty": 0}
            )
            target["types"] = sorted(set(target["types"] + stat["types"]))
            for key in ("occurrences", "null", "empty"):
                target[key] += stat[key]
    fields = {}
    for path in expected:
        stat = inventory.get(path)
        fields[path] = (
            "not_returned"
            if stat is None
            else "empty_or_null_only"
            if stat["occurrences"] == stat["null"] + stat["empty"]
            else "observed"
        )
    return {"documented_fields": fields, "field_inventory": inventory}


class Capture:
    """一个客户端、一个记录目录。请求白名单只包含此次实验的只读接口。"""

    def __init__(
        self, settings: Settings, client: httpx.AsyncClient, folder: Path, timeout=45
    ):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive and finite")
        self.settings = settings
        self.client = client
        self.folder = folder
        self.timeout = timeout
        self.records = []
        folder.mkdir(parents=True, exist_ok=True)

    def write(self, name: str, value):
        text = dumps(
            redact(value, (self.settings.amap_api_key, self.settings.tuniu_api_key))
        )
        (self.folder / name).write_text(text + "\n", encoding="utf-8")

    async def tuniu(self, service, tool, arguments, expected=(), parent_id=None):
        if service not in READ_TOOLS or tool not in READ_TOOLS[service] | {
            "tools/list"
        }:
            raise ValueError("Only documented read-only tools are allowed")
        endpoint = getattr(self.settings, f"tuniu_{service}_url")
        request_id = uuid4().hex
        request = {"jsonrpc": "2.0", "id": request_id, "method": "tools/list"}
        if tool != "tools/list":
            request.update(
                method="tools/call", params={"name": tool, "arguments": arguments}
            )
        elif arguments:
            request["params"] = arguments
        return await self._request(
            f"{service}.{tool}",
            endpoint,
            arguments,
            expected,
            parent_id,
            rpc_id=request_id,
            json=request,
            headers={
                "apiKey": self.settings.tuniu_api_key or "",
                "Accept": "application/json, text/event-stream",
            },
        )

    async def amap(self, operation, arguments, expected=(), parent_id=None):
        path = AMAP_PATHS[operation]
        endpoint = "https://restapi.amap.com" + path
        return await self._request(
            f"amap.{operation}",
            endpoint,
            arguments,
            expected,
            parent_id,
            params={
                **arguments,
                "key": self.settings.amap_api_key or "",
                "output": "JSON",
            },
        )

    async def _request(
        self, operation, endpoint, arguments, expected, parent_id, rpc_id=None, **kwargs
    ):
        record = {
            "call_id": uuid4().hex,
            "parent_call_id": parent_id,
            "operation": operation,
            "endpoint": endpoint,
            "arguments": arguments,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "external_text_trust": "untrusted",
            "status": "error",
            "decimal_encoding": "JSON decimal numbers are serialized as exact strings; see field_inventory types",
        }
        started = perf_counter()
        body = bytearray()
        try:
            async with asyncio.timeout(self.timeout):
                async with self.client.stream(
                    "POST" if rpc_id else "GET", endpoint, **kwargs
                ) as response:
                    record["http_status"] = response.status_code
                    is_sse = "text/event-stream" in response.headers.get(
                        "content-type", ""
                    )
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_BYTES:
                            raise ValueError(
                                "Response exceeds 16 MiB; not treated as complete"
                            )
                        if is_sse:
                            events, trailing = complete_sse(bytes(body))
                            if any(
                                isinstance(e, dict)
                                and e.get("id") == rpc_id
                                and ("result" in e or "error" in e)
                                for e in events
                            ):
                                break
                    record["response_bytes"] = len(body)
                    # wire_text 是解析审计依据，不包含请求头；完整保留数值的原始写法。
                    record["wire_text"] = body.decode("utf-8", errors="replace")
                    if is_sse:
                        envelopes, trailing = complete_sse(bytes(body))
                        record["sse_trailing_text"] = trailing.decode(
                            "utf-8", errors="replace"
                        )
                    else:
                        envelopes = decode_wire(bytes(body), False)
                    record["envelopes"] = envelopes
                    if rpc_id:
                        matches = [
                            e
                            for e in envelopes
                            if isinstance(e, dict)
                            and e.get("id") == rpc_id
                            and ("result" in e or "error" in e)
                        ]
                        if len(matches) != 1 or matches[0].get("jsonrpc") != "2.0":
                            raise ValueError(
                                "Missing or ambiguous matching JSON-RPC response"
                            )
                        envelope = matches[0]
                        payloads = unpack(envelope)
                    else:
                        if len(envelopes) != 1 or not isinstance(envelopes[0], dict):
                            raise ValueError("Expected a JSON response object")
                        envelope = envelopes[0]
                        payloads = [envelope]
                    record["payloads"] = payloads
                    record.update(check_expected(payloads, list(expected)))
                    record["collection_sizes"] = {
                        key: len(value)
                        for item in payloads
                        if isinstance(item, dict)
                        for key, value in item.items()
                        if isinstance(value, list)
                    }
                    response.raise_for_status()
                    if "error" in envelope or (
                        isinstance(envelope.get("result"), dict)
                        and envelope["result"].get("isError") is True
                    ):
                        raise ValueError(
                            "Supplier JSON-RPC/tool error; see captured response"
                        )
                    for payload in payloads:
                        if isinstance(payload, dict) and (
                            payload.get("success") is False
                            or payload.get("successCode") is False
                        ):
                            raise ValueError(
                                "Supplier business error; see captured response"
                            )
                    if not rpc_id and envelope.get("status") != "1":
                        raise ValueError("Amap business error; see captured response")
                    if rpc_id and not operation.endswith("getBookingRequiredInfo"):
                        key, kind = {
                            "tools/list": ("tools", list),
                            "searchLowestPriceFlight": ("data", list),
                            "multiCabinDetails": ("cabinInfo", list),
                            "searchLowestPriceTrain": ("data", list),
                            "queryTrainDetail": ("data", dict),
                            "tuniuHotelSearch": ("hotels", list),
                            "tuniuHotelDetail": ("roomTypes", list),
                        }[operation.split(".", 1)[1]]
                        if not any(isinstance(p, dict) and isinstance(p.get(key), kind) for p in payloads):
                            raise ValueError("Missing expected business result container")
                    record["status"] = "ok"
        except (
            httpx.HTTPError,
            TimeoutError,
            ValueError,
            TypeError,
            UnicodeError,
        ) as exc:
            # HTTP 异常字符串可能带 URL query/key，控制台和 error 中只保存类型。
            record["error_type"] = type(exc).__name__
            if isinstance(exc, ValueError) and not isinstance(
                exc, json.JSONDecodeError
            ):
                record["error"] = str(exc)
        finally:
            if body:
                record["wire_text"] = body.decode("utf-8", errors="replace")
                record["response_bytes"] = len(body)
        record["elapsed_seconds"] = round(perf_counter() - started, 3)
        record["file"] = (
            f"{len(self.records) + 1:03d}_{operation.replace('/', '_')}.json"
        )
        self.write(record["file"], record)
        self.records.append(record)
        print(
            f"{operation}: {record['status']} ({record['elapsed_seconds']}s)",
            flush=True,
        )
        return record


def payload(record) -> dict:
    """仅供实验流程取后续请求参数；原始全部结果始终保存在记录中。"""
    if record["status"] != "ok":
        return {}
    return next(
        (
            item
            for item in record.get("payloads", [])
            if isinstance(item, dict)
            and "text" not in item
            and not isinstance(item.get("result"), str)
        ),
        {},
    )
