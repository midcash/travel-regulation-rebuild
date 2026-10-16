"""MCP 的消息形状与错误码。对应笔记 4.4 节引的官方规范。

MCP 跑在 JSON-RPC 2.0 之上。三个方法撑起工具能力：

    initialize    握手：交换协议版本和能力声明
    tools/list    动态发现：服务端告诉客户端"我现在有哪些工具"
    tools/call    执行：客户端请求调用某个工具

**动态发现是 MCP 和 Function Calling 最本质的差别**（笔记 4.1）：
Function Calling 的工具列表是在会话上下文中静态配置的，
而 MCP 的 tools/list 让客户端在运行时才知道有哪些工具、参数长什么样。
服务端改一个工具的参数，客户端不用改代码。

错误分两层（笔记 4.4 第 3 条）：
    协议级错误 -> JSON-RPC 的 error 字段，带标准 code
    业务级错误 -> result 里 isError: true + 一段给 LLM 看的说明
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

PROTOCOL_VERSION = "2025-06-18"

# JSON-RPC 2.0 标准错误码
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# MCP 规范里 tools/call 遇到"工具不存在"和"参数不合法"都用 -32602，
# 因为对客户端来说这两件事都是"你这次请求不对，改一下再来"。
TOOL_NOT_FOUND = INVALID_PARAMS
BAD_ARGUMENTS = INVALID_PARAMS


@dataclass
class Request:
    method: str
    params: dict[str, Any] = field(default_factory=dict)
    id: int | str | None = None

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": self.method}
        if self.params:
            payload["params"] = self.params
        if self.id is not None:
            payload["id"] = self.id
        return payload

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Request":
        return cls(method=data.get("method", ""), params=data.get("params") or {}, id=data.get("id"))


@dataclass
class Error:
    code: int
    message: str
    data: Any = None

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            payload["data"] = self.data
        return payload


@dataclass
class Response:
    id: int | str | None
    result: dict[str, Any] | None = None
    error: Error | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": self.id}
        if self.error is not None:
            payload["error"] = self.error.to_json()
        else:
            payload["result"] = self.result or {}
        return payload

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Response":
        raw_error = data.get("error")
        error = (
            Error(raw_error.get("code", INTERNAL_ERROR), raw_error.get("message", ""), raw_error.get("data"))
            if raw_error
            else None
        )
        return cls(id=data.get("id"), result=data.get("result"), error=error)


def text_content(text: str) -> dict[str, Any]:
    """MCP 的 content 是数组，每个元素带 type。纯文本就是 type=text。"""
    return {"type": "text", "text": text}


def tool_descriptor(
    name: str,
    description: str,
    input_schema: dict[str, Any],
    *,
    output_schema: dict[str, Any] | None = None,
    annotations: dict[str, bool] | None = None,
) -> dict[str, Any]:
    """tools/list 里的一项。

    注意字段名和 OpenAI tools 不一样：MCP 用 inputSchema / outputSchema，
    OpenAI 用 function.parameters。这一层差异由客户端负责抹平 ——
    host.py 里做的就是这件事。
    """
    descriptor: dict[str, Any] = {
        "name": name,
        "description": description,
        "inputSchema": input_schema,
    }
    if output_schema:
        descriptor["outputSchema"] = output_schema
    if annotations:
        descriptor["annotations"] = annotations
    return descriptor
