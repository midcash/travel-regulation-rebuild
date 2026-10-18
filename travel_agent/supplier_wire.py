"""Supplier JSON/SSE decoding migrated from the current experiment-two adapter."""
import json
import re
from decimal import Decimal

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
                if not isinstance(text, str):
                    raise TypeError("Expected text content")
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


