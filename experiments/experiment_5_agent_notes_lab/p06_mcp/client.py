"""MCP Client：负责握手、动态发现工具、发起调用。

两种传输都实现了，形状完全一样，差别只在"消息怎么过去"：

    InProcessTransport  同一个进程里直连。
                        调试时用这个 —— 客户端和服务端的断点都在一个进程里，
                        单步能一路跟过去，看得见每一层。

    StdioTransport      把 server.py 起成子进程，走 stdin/stdout。
                        这才是本地 MCP Server 的真实形态（笔记 4.3）。

两者跑的是同一套 JSON-RPC 消息，所以换传输不影响上层代码 ——
这也正是 MCP "接口标准化"的价值所在。
"""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from protocol import Error, Request, Response
from server import MCPServer


@dataclass
class ToolCallResult:
    """一次 tools/call 的结果。两层错误分得很清楚。"""

    ok: bool
    text: str = ""
    structured: dict[str, Any] = field(default_factory=dict)
    is_error: bool = False  # 业务级错误（isError: true）
    protocol_error: Error | None = None  # 协议级错误（JSON-RPC error）
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def error_kind(self) -> str:
        if self.protocol_error is not None:
            return "协议级"
        if self.is_error:
            return "业务级"
        return "无"


class Transport(Protocol):
    def send(self, request: Request) -> Response: ...
    def close(self) -> None: ...


class InProcessTransport:
    """同进程直连。调试首选。"""

    name = "in-process"

    def __init__(self, server: MCPServer):
        self.server = server

    def send(self, request: Request) -> Response:
        return self.server.handle(request)

    def close(self) -> None:
        pass


class StdioTransport:
    """子进程 + 标准输入输出。本地 MCP Server 的真实形态。"""

    name = "stdio"

    def __init__(self, script: Path | str | None = None):
        self.script = Path(script or Path(__file__).resolve().parent / "server.py")
        self.process: subprocess.Popen[str] | None = None
        self.stderr_tail: list[str] = []

    def start(self) -> None:
        # stderr 单独收着：stdout 是协议通道，服务端的日志只能走 stderr
        import os

        env = dict(os.environ)
        # 双保险：server.py 自己会 reconfigure，这里再从外部钉一次，
        # 免得子进程有任何一行输出跑成本地编码，把协议通道污染掉
        env["PYTHONIOENCODING"] = "utf-8"
        self.process = subprocess.Popen(
            [sys.executable, str(self.script)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=env,
            cwd=str(self.script.parent),
        )

    def send(self, request: Request) -> Response:
        if self.process is None or self.process.stdin is None or self.process.stdout is None:
            raise RuntimeError("StdioTransport 还没 start()")
        self.process.stdin.write(json.dumps(request.to_json(), ensure_ascii=False) + "\n")
        self.process.stdin.flush()
        line = self.process.stdout.readline()
        if not line:
            raise RuntimeError("子进程没有返回任何内容，可能已经退出")
        return Response.from_json(json.loads(line))

    def close(self) -> None:
        if self.process is None:
            return
        if self.process.stdin:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover
            self.process.kill()
        if self.process.stderr:
            self.stderr_tail = self.process.stderr.read().splitlines()


class MCPClient:
    """一个客户端连接一个 Server（笔记 4.1：MCP 是客户端-服务端架构）。"""

    def __init__(self, transport: Transport, name: str = "mcp-client"):
        self.transport = transport
        self.name = name
        self._next_id = 0
        self.server_info: dict[str, Any] = {}
        self.tools: list[dict[str, Any]] = []
        self.round_trips: list[dict[str, Any]] = []

    def _call(self, method: str, params: dict[str, Any] | None = None) -> Response:
        self._next_id += 1
        request = Request(method=method, params=params or {}, id=self._next_id)
        response = self.transport.send(request)
        self.round_trips.append(
            {
                "id": request.id,
                "method": method,
                "ok": response.ok,
                "error": response.error.message if response.error else "",
            }
        )
        return response

    # -- 三步走 -----------------------------------------------------------

    def initialize(self) -> dict[str, Any]:
        response = self._call(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": self.name, "version": "1.0.0"},
            },
        )
        self.server_info = response.result or {}
        return self.server_info

    def list_tools(self) -> list[dict[str, Any]]:
        """动态发现。客户端不需要提前知道服务端有哪些工具。"""
        response = self._call("tools/list")
        self.tools = (response.result or {}).get("tools", [])
        return self.tools

    def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolCallResult:
        response = self._call("tools/call", {"name": name, "arguments": arguments})
        if not response.ok:
            return ToolCallResult(ok=False, protocol_error=response.error, raw=response.to_json())

        result = response.result or {}
        contents = result.get("content") or []
        text = "\n".join(item.get("text", "") for item in contents if item.get("type") == "text")
        is_error = bool(result.get("isError"))
        return ToolCallResult(
            ok=not is_error,
            text=text,
            structured=result.get("structuredContent") or {},
            is_error=is_error,
            raw=result,
        )

    def close(self) -> None:
        self.transport.close()
