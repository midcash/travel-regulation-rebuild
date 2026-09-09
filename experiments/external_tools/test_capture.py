"""事先验收条件：完整字段、十进制精度、响应关联、失败不能伪装成功。"""

import asyncio
import json
from decimal import Decimal

import httpx
import pytest

from .capture import (
    Capture,
    check_expected,
    decode_wire,
    dumps,
    loads,
    payload,
    unpack,
    redact,
)
from .config import Settings


def test_preserve_all_fields_and_precise_numbers():
    source = '{"price":123.450000000000000001,"id":9007199254740993,"unknown":{"items":[null,"",false,0,{"nested":"保留"}]}}'
    data = loads(source)
    assert data["price"] == Decimal("123.450000000000000001")
    assert data["id"] == 9007199254740993
    assert data["unknown"]["items"] == [None, "", False, 0, {"nested": "保留"}]
    saved = json.loads(dumps(data))
    assert saved["price"] == "123.450000000000000001"
    assert saved["unknown"] == data["unknown"]


def test_missing_empty_and_zero_are_distinct():
    result = check_expected(
        [{"items": [{"x": None}, {"x": "", "zero": 0, "flag": False}]}],
        ["$.items[].x", "$.items[].missing", "$.items[].zero", "$.items[].flag"],
    )
    assert result["documented_fields"] == {
        "$.items[].x": "empty_or_null_only",
        "$.items[].missing": "not_returned",
        "$.items[].zero": "observed",
        "$.items[].flag": "observed",
    }
    assert result["field_inventory"]["$.items[].x"]["occurrences"] == 2


def test_structured_wrapper_does_not_hide_actual_payload():
    body = '{"successCode":true,"data":[{"departureAirport":"首都","extra":12.34}]}'
    envelope = {
        "result": {
            "structuredContent": {"result": body},
            "content": [
                {"type": "text", "text": body},
                {"type": "text", "text": "说明文字"},
                {"type": "resource_link", "uri": "https://example.com"},
            ],
        }
    }
    decoded = unpack(envelope)
    selected = payload({"status": "ok", "payloads": decoded})
    assert selected["data"][0] == {
        "departureAirport": "首都",
        "extra": Decimal("12.34"),
    }
    assert {"text": "说明文字"} in decoded
    assert {"type": "resource_link", "uri": "https://example.com"} in decoded
    assert {"result": body} in decoded


def test_json_containing_data_colon_is_not_sse():
    assert decode_wire(b'{"url":"data:hello"}', False) == [{"url": "data:hello"}]


def test_malformed_json_is_not_successful_plain_text():
    with pytest.raises(ValueError, match="Malformed JSON"):
        unpack({"result": {"content": [{"type": "text", "text": '{"price":1.2'}]}})


def test_sse_comments_multiline_and_notifications():
    wire = b': heartbeat\r\n\r\ndata: {"method":"notice"}\r\n\r\nevent: message\r\ndata: {"jsonrpc":"2.0",\r\ndata: "id":"abc","result":{"content":[]}}\r\n\r\n'
    assert decode_wire(wire, True) == [
        {"method": "notice"},
        {"jsonrpc": "2.0", "id": "abc", "result": {"content": []}},
    ]


def test_complete_sse_result_before_partial_heartbeat(tmp_path):
    class Stream(httpx.AsyncByteStream):
        def __init__(self, request_id):
            self.request_id = request_id

        async def __aiter__(self):
            message = dumps(
                {
                    "jsonrpc": "2.0",
                    "id": self.request_id,
                    "result": {"successCode": True, "data": [{"airport": "首都"}]},
                }
            )
            yield ("data: " + message.replace("\n", "\ndata: ") + "\n\n: keep").encode()
            raise httpx.ReadTimeout("must not wait for another chunk")

    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=Stream(json.loads(request.content)["id"]),
        )

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            record = await Capture(Settings(), client, tmp_path).tuniu(
                "flight", "searchLowestPriceFlight", {}
            )
            assert record["status"] == "ok"
            assert payload(record)["data"] == [{"airport": "首都"}]
            assert record["sse_trailing_text"] == ": keep"
            assert record["wire_text"].endswith(": keep")

    asyncio.run(scenario())


def test_partial_response_is_retained_on_timeout(tmp_path):
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"unfinished":'
            raise httpx.ReadTimeout("timeout")

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, stream=Stream())
            )
        ) as client:
            record = await Capture(Settings(), client, tmp_path).tuniu(
                "flight", "searchLowestPriceFlight", {}
            )
            assert record["status"] == "error"
            assert record["wire_text"] == '{"unfinished":'

    asyncio.run(scenario())


def test_unicode_escaped_credentials_are_redacted(tmp_path):
    source = {
        "wire_text": r'{"message":"\u0074est-secret"}',
        "nested": [{"text": r"test-secret and \\u0074est-secret"}],
    }
    cleaned = redact(source, ("test-secret",))
    assert cleaned["wire_text"] == "[REDACTED_TEXT_CONTAINING_API_KEY]"
    assert "secret" not in dumps(cleaned)
    Capture(Settings(tuniu_api_key="test-secret"), None, tmp_path).write(
        "redaction.json", source
    )
    assert loads((tmp_path / "redaction.json").read_text(encoding="utf-8")) == cleaned


@pytest.mark.parametrize("failure", ["rpc", "business", "http", "wrong_id", "timeout", "plain_error"])
def test_errors_remain_errors(tmp_path, failure):
    def handler(request):
        rpc = json.loads(request.content)
        if failure == "timeout":
            raise httpx.ReadTimeout("secret-url", request=request)
        response = {
            "jsonrpc": "2.0",
            "id": rpc["id"],
            "result": {"successCode": True, "data": []},
        }
        if failure == "rpc":
            response = {
                "jsonrpc": "2.0",
                "id": rpc["id"],
                "error": {"code": -1, "message": "test-secret"},
            }
        elif failure == "business":
            response["result"]["successCode"] = False
        elif failure == "wrong_id":
            response["id"] = "different"
        elif failure == "plain_error":
            response["result"] = {"content": [{"type": "text", "text": "服务暂时不可用"}]}
        return httpx.Response(503 if failure == "http" else 200, json=response)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            capture = Capture(Settings(tuniu_api_key="test-secret"), client, tmp_path)
            record = await capture.tuniu("flight", "searchLowestPriceFlight", {})
            assert record["status"] == "error"
            saved = (tmp_path / record["file"]).read_text(encoding="utf-8")
            assert "test-secret" not in saved
            assert "secret-url" not in saved

    asyncio.run(scenario())


def test_successful_capture_keeps_every_item(tmp_path):
    def handler(request):
        rpc = json.loads(request.content)
        assert request.headers["apiKey"] == "private-key"
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": rpc["id"],
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": '{"successCode":true,"data":[{"x":1},{"x":2,"newField":{"a":null}}]}',
                        }
                    ]
                },
            },
        )

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            capture = Capture(Settings(tuniu_api_key="private-key"), client, tmp_path)
            record = await capture.tuniu(
                "flight", "searchLowestPriceFlight", {"departureDate": "2026-09-12"}
            )
            assert record["status"] == "ok"
            assert payload(record)["data"] == [
                {"x": 1},
                {"x": 2, "newField": {"a": None}},
            ]
            saved = loads((tmp_path / record["file"]).read_text(encoding="utf-8"))
            assert saved["arguments"] == {"departureDate": "2026-09-12"}
            assert "private-key" not in dumps(saved)
            with pytest.raises(ValueError, match="read-only"):
                await capture.tuniu("flight", "saveOrder", {})

    asyncio.run(scenario())


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_non_json_numbers_are_rejected(value):
    with pytest.raises(ValueError):
        loads('{"price":' + value + "}")
