"""一个最小的 MCP Server。对应笔记 4.4 节的六条工具设计原则。

三个工具按"先发现 -> 再了解 -> 最后查询"的自然工作流拆：

    list_collections     列出知识库里有哪些集合
    describe_collection  看某个集合有哪些字段
    query_knowledge      真正查询

这就是笔记 4.4 第 5 条说的**粒度控制**：太粗模型无法灵活组合，
太细调用链路过长容易出错。上游工具的输出正好是下游工具的输入参数。

六条原则在代码里的落点：
    第 1 条 入参设计        必填少、有默认值、enum 收敛、约束内联（看 _QUERY_SCHEMA）
    第 2 条 出参设计        outputSchema + 一段 LLM 友好的纯文本
    第 3 条 错误处理        协议级走 JSON-RPC error，业务级走 isError
    第 4 条 ToolAnnotations 四个标注都写上了
    第 5 条 粒度控制        三个工具形成一条工作流
    第 6 条 安全与校验      schema 校验之外，服务端还要做业务层校验

服务端本身不依赖任何 MCP SDK —— 它只是一段"从 stdin 读 JSON 行、往 stdout 写 JSON 行"
的循环。把协议摊开写一遍，比调 SDK 更能看清中间发生了什么。
"""
from __future__ import annotations

import json
import sys
from typing import Any, Callable

from protocol import (
    BAD_ARGUMENTS,
    INTERNAL_ERROR,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PROTOCOL_VERSION,
    TOOL_NOT_FOUND,
    Error,
    Request,
    Response,
    text_content,
    tool_descriptor,
)

# --------------------------------------------------------------------------
# 假知识库：服务端"背后"的数据
# --------------------------------------------------------------------------

KNOWLEDGE_BASE: dict[str, dict[str, Any]] = {
    "travel_policy": {
        "title": "差旅报销制度",
        "documents": {
            "refund": "退票规定：开车前 24 小时以上退票免手续费；开车前 24 小时内收取 20% 手续费。",
            "invoice": "发票规定：报销发票抬头必须写「某某科技有限公司」，税号 91310000MA1FL1234X。",
            "hotel": "住宿标准：一线城市每晚上限 600 元，二线城市 400 元，需附水单。",
        },
    },
    "member_rules": {
        "title": "会员权益",
        "documents": {
            "platinum": "白金卡会员：退票免手续费，每年 6 次免费改签，可享贵宾厅。",
            "gold": "金卡会员：退票收 10% 手续费，每年 3 次免费改签。",
        },
    },
}

# 第 1 条：入参设计。必填只有两个，能给默认值的都给上，用 enum 收敛，约束内联。
_QUERY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "collection": {
            "type": "string",
            "description": "要查询的集合名。不确定有哪些集合时，先调用 list_collections。",
            "enum": sorted(KNOWLEDGE_BASE),
        },
        "question": {"type": "string", "description": "要查的问题，用自然语言描述"},
        "top_k": {
            "type": "integer",
            "description": "返回条数",
            "default": 2,
            "minimum": 1,
            "maximum": 5,
        },
    },
    "required": ["collection", "question"],
}


class MCPServer:
    """按方法名分发的 JSON-RPC 服务端。"""

    def __init__(self, name: str = "travel-kb", version: str = "1.0.0"):
        self.name = name
        self.version = version
        self.handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "initialize": self._initialize,
            "tools/list": self._tools_list,
            "tools/call": self._tools_call,
        }
        self.call_log: list[dict[str, Any]] = []

    # -- JSON-RPC 入口 ----------------------------------------------------

    def handle(self, request: Request) -> Response:
        """分发一个请求。永远返回 Response，不抛异常 —— 异常也要变成 JSON-RPC error。"""
        handler = self.handlers.get(request.method)
        if handler is None:
            return Response(
                id=request.id,
                error=Error(METHOD_NOT_FOUND, f"不支持的方法 {request.method!r}"),
            )
        try:
            result = handler(request.params)
        except ProtocolError as exc:
            # 协议级错误：带上标准 JSON-RPC code
            return Response(id=request.id, error=Error(exc.code, exc.message, exc.data))
        except Exception as exc:  # noqa: BLE001 - 服务端不能因为一个请求崩掉
            return Response(id=request.id, error=Error(INTERNAL_ERROR, f"{type(exc).__name__}: {exc}"))
        return Response(id=request.id, result=result)

    # -- 三个方法 ---------------------------------------------------------

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        """握手：交换协议版本和能力声明。"""
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": True}},
            "serverInfo": {"name": self.name, "version": self.version},
        }

    def _tools_list(self, params: dict[str, Any]) -> dict[str, Any]:
        """动态发现。客户端不需要提前知道这里有哪三个工具。"""
        return {"tools": self.descriptors()}

    def descriptors(self) -> list[dict[str, Any]]:
        return [
            tool_descriptor(
                "list_collections",
                "列出知识库里所有可查询的集合。不确定有哪些集合时先调用它。",
                {"type": "object", "properties": {}, "required": []},
                output_schema={
                    "type": "object",
                    "properties": {"collections": {"type": "array", "items": {"type": "string"}}},
                },
                # 第 4 条：ToolAnnotations。四个标注一个不少。
                annotations={
                    "readOnlyHint": True,
                    "destructiveHint": False,
                    "idempotentHint": True,
                    "openWorldHint": False,
                },
            ),
            tool_descriptor(
                "describe_collection",
                "查看某个集合里有哪些文档以及各自的用途。查询前先了解结构，能提高命中率。",
                {
                    "type": "object",
                    "properties": {
                        "collection": {
                            "type": "string",
                            "description": "集合名",
                            "enum": sorted(KNOWLEDGE_BASE),
                        }
                    },
                    "required": ["collection"],
                },
                annotations={
                    "readOnlyHint": True,
                    "destructiveHint": False,
                    "idempotentHint": True,
                    "openWorldHint": False,
                },
            ),
            tool_descriptor(
                "query_knowledge",
                "在指定集合里检索答案。返回带来源引用的可读文本，不是裸 JSON。",
                _QUERY_SCHEMA,
                output_schema={
                    "type": "object",
                    "properties": {
                        "hits": {"type": "array"},
                        "collection": {"type": "string"},
                    },
                },
                annotations={
                    "readOnlyHint": True,
                    "destructiveHint": False,
                    "idempotentHint": True,
                    "openWorldHint": False,
                },
            ),
        ]

    def _tools_call(self, params: dict[str, Any]) -> dict[str, Any]:
        """执行一次工具调用。两层错误就分在这里。"""
        name = params.get("name")
        arguments: dict[str, Any] = params.get("arguments") or {}
        self.call_log.append({"name": name, "arguments": arguments})

        handler = {
            "list_collections": self._list_collections,
            "describe_collection": self._describe_collection,
            "query_knowledge": self._query_knowledge,
        }.get(str(name))

        # ---- 协议级错误：工具不存在 ----
        if handler is None:
            raise ProtocolError(
                TOOL_NOT_FOUND,
                f"没有名为 {name!r} 的工具",
                {"availableTools": [d["name"] for d in self.descriptors()]},
            )

        # ---- 协议级错误：参数不合法 ----
        schema = next(d["inputSchema"] for d in self.descriptors() if d["name"] == name)
        cleaned, missing, bad = _validate(schema, arguments)
        if missing or bad:
            detail = []
            if missing:
                detail.append(f"缺少必填参数 {missing}")
            if bad:
                detail.append(f"参数非法 {bad}")
            raise ProtocolError(
                BAD_ARGUMENTS,
                "；".join(detail),
                {"failedFields": missing + bad, "expectedSchema": schema},
            )

        # ---- 业务级错误：isError: true ----
        try:
            payload = handler(**cleaned)
        except BusinessError as exc:
            return {
                "content": [text_content(f"查询失败：{exc.message}")],
                "isError": True,
                "_businessError": True,
            }

        text, structured = _shape(payload)
        return {"content": [text_content(text)], "structuredContent": structured, "isError": False}

    # -- 工具实现 ---------------------------------------------------------

    def _list_collections(self) -> dict[str, Any]:
        return {
            "collections": [
                {"name": name, "title": data["title"], "documentCount": len(data["documents"])}
                for name, data in KNOWLEDGE_BASE.items()
            ]
        }

    def _describe_collection(self, collection: str) -> dict[str, Any]:
        data = KNOWLEDGE_BASE.get(collection)
        if data is None:
            # 业务级错误：告诉 LLM 下一步怎么办（笔记 4.4 第 3 条）
            raise BusinessError(
                f"集合 {collection!r} 不存在，可用的集合有 {sorted(KNOWLEDGE_BASE)}"
            )
        return {
            "collection": collection,
            "title": data["title"],
            "documents": [{"key": key, "summary": value[:24] + "…"} for key, value in data["documents"].items()],
        }

    def _query_knowledge(self, collection: str, question: str, top_k: int = 2) -> dict[str, Any]:
        data = KNOWLEDGE_BASE.get(collection)
        if data is None:
            # 第 6 条：即使 schema 校验过了（enum 挡住了），服务端仍然要业务校验。
            # 这里保留这条分支是为了演示"两层校验都要有"，正常走不到。
            raise BusinessError(f"集合 {collection!r} 不存在，可用的集合有 {sorted(KNOWLEDGE_BASE)}")

        documents = data["documents"]
        keywords = [ch for ch in question if "一" <= ch <= "鿿"]
        scored = []
        for key, value in documents.items():
            score = sum(1 for ch in keywords if ch in value)
            if score:
                scored.append((score, key, value))
        scored.sort(reverse=True)

        if not scored:
            raise BusinessError(
                f"在集合 {collection!r} 里没有找到和 {question!r} 相关的内容。"
                f"该集合包含这些主题：{sorted(documents)}"
            )

        hits = [
            {"key": key, "text": value, "confidence": round(score / max(1, len(keywords)), 2), "source": f"{collection}/{key}"}
            for score, key, value in scored[:top_k]
        ]
        return {"collection": collection, "question": question, "hits": hits}


# --------------------------------------------------------------------------
# 错误与校验
# --------------------------------------------------------------------------


class ProtocolError(Exception):
    """协议级错误：会被转成 JSON-RPC 的 error 字段。"""

    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class BusinessError(Exception):
    """业务级错误：会被转成 isError: true 的结果。"""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


_TYPE_MAP: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list, tuple),
    "object": (dict,),
}


def _validate(schema: dict[str, Any], arguments: dict[str, Any]) -> tuple[dict[str, Any], list[str], list[str]]:
    """轻量 schema 校验 + 默认值填充。和第 1 条原则配套。"""
    properties: dict[str, Any] = schema.get("properties", {})
    required: list[str] = list(schema.get("required", []))
    cleaned: dict[str, Any] = {}

    for key, spec in properties.items():
        if key not in arguments and "default" in spec:
            cleaned[key] = spec["default"]

    missing: list[str] = []
    bad: list[str] = []
    for key, value in arguments.items():
        if key not in properties:
            bad.append(key)
        else:
            cleaned[key] = value

    for key in required:
        if key not in cleaned:
            missing.append(key)

    for key, value in cleaned.items():
        if key in missing:
            continue
        spec = properties.get(key, {})
        expected = spec.get("type")
        if expected in _TYPE_MAP:
            if expected in {"integer", "number"} and isinstance(value, bool):
                bad.append(key)
                continue
            if not isinstance(value, _TYPE_MAP[expected]):
                bad.append(key)
                continue
        if "enum" in spec and value not in spec["enum"]:
            bad.append(key)
        if isinstance(value, int) and not isinstance(value, bool):
            if "minimum" in spec and value < spec["minimum"]:
                bad.append(key)
            if "maximum" in spec and value > spec["maximum"]:
                bad.append(key)

    return cleaned, missing, bad


def _shape(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """第 2 条：结构化返回 + 一段 LLM 友好的纯文本。

    规范要求"结构化返回的同时要附带一个纯文本版本，保证老 Client 也能读"。
    LLM 读自然语言比读嵌套 JSON 效率高得多。
    """
    lines: list[str] = []
    for key, value in payload.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            for item in value:
                if isinstance(item, dict):
                    lines.append("  - " + "；".join(f"{k}={v}" for k, v in item.items()))
                else:
                    lines.append(f"  - {item}")
        else:
            lines.append(f"{key}: {value}")
    return "\n".join(lines), payload


# --------------------------------------------------------------------------
# stdio 传输：真正跑起来的那条路
# --------------------------------------------------------------------------


def serve_stdio(server: MCPServer | None = None) -> None:
    """从 stdin 逐行读 JSON-RPC 请求，往 stdout 写响应。

    这就是本地 MCP Server 的 stdio 传输（笔记 4.3）：
    不涉及网络、不需要认证，进程间用标准输入输出对话。

    两个坑都在这一小段里，而且都是自己实现 MCP Server 时真会踩到的：

      1. 日志一律写 stderr。stdout 是协议通道，混进一行非 JSON 客户端就解析失败。
      2. **stdin/stdout 必须显式设成 UTF-8。** Windows 上子进程的默认编码是
         本地代码页（cp936），写中文会编成 GBK；父进程按 UTF-8 解码直接抛
         UnicodeDecodeError。这个错误只在跨进程时才出现，同进程调试永远看不到。
    """
    try:
        sys.stdin.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, ValueError):  # pragma: no cover
        pass

    server = server or MCPServer()
    print(f"[server] {server.name} 已启动，等待 stdio 请求", file=sys.stderr)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError as exc:
            response = Response(id=None, error=Error(-32700, f"JSON 解析失败：{exc}"))
        else:
            response = server.handle(Request.from_json(data))
        sys.stdout.write(json.dumps(response.to_json(), ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    serve_stdio()
