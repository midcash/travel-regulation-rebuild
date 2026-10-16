"""Host：跑 LLM 的那个应用，也是 MCP 三层架构的最外层。

笔记 4.1：MCP 采用客户端-服务端架构，三个关键组件是 Host / Client / Server，
其中 Host 是运行 LLM 的应用程序。

Host 要干一件 Client 不管的事：**把 MCP 的工具描述翻译成模型认识的函数定义**。
两边的字段名不一样：

    MCP 的 tools/list          ->  模型要的 function 定义
    {                              {
      "name": ...,                   "type": "function",
      "description": ...,             "function": {
      "inputSchema": {...}              "name": ...,
    }                                   "description": ...,
                                        "parameters": {...}    <- inputSchema 挪到这
                                      }
                                    }

这层翻译就是 host.py 存在的全部理由。有了它，模型侧完全不需要知道
对面是 MCP 还是本地函数 —— 这正是笔记 4.1 说的"接口标准化，减少开发成本"。
"""
from __future__ import annotations

from typing import Any

from client import MCPClient


def mcp_tools_as_functions(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """MCP 工具描述 -> 模型能用的 function 定义。"""
    functions: list[dict[str, Any]] = []
    for tool in tools:
        functions.append(
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("inputSchema", {"type": "object", "properties": {}}),
                },
            }
        )
    return functions


class MCPToolkit:
    """把 MCP Client 包装成本地工具注册表的样子。

    对 ReAct 循环来说，这个对象和 common.tools.ToolRegistry 没有区别 ——
    这正是"标准化"想要的效果。
    """

    def __init__(self, client: MCPClient):
        self.client = client
        self.functions: list[dict[str, Any]] = []
        self.annotations: dict[str, dict[str, bool]] = {}
        self.output_schemas: dict[str, dict[str, Any]] = {}

    def refresh(self) -> list[dict[str, Any]]:
        """重新发现工具。服务端加了新工具或改了参数，这里一刷就能看到。"""
        tools = self.client.list_tools()
        self.functions = mcp_tools_as_functions(tools)
        self.annotations = {t["name"]: t.get("annotations", {}) for t in tools}
        self.output_schemas = {t["name"]: t.get("outputSchema", {}) for t in tools}
        return self.functions

    def as_registry(self) -> Any:
        """转成 common.tools.ToolRegistry，好和别的原型共用一套循环。"""
        from common.tools import Tool, ToolRegistry

        registry = ToolRegistry("mcp")
        client = self.client
        for function in self.functions:
            fn = function["function"]
            registry.register(
                Tool(
                    name=fn["name"],
                    description=fn["description"],
                    parameters=fn["parameters"],
                    handler=_make_handler(client, fn["name"]),
                    annotations=_normalize_annotations(self.annotations.get(fn["name"], {})),
                    output_schema=self.output_schemas.get(fn["name"]),
                )
            )
        return registry


def _make_handler(client: MCPClient, tool_name: str):
    def handler(**arguments: Any) -> Any:
        result = client.call_tool(tool_name, arguments)
        if result.protocol_error is not None:
            # MCP 的协议级错误转成本地的协议级错误，语义完全一致
            from common.tools import ToolFailure

            raise ToolFailure(
                f"MCP_PROTOCOL_{result.protocol_error.code}",
                result.protocol_error.message,
                retryable=False,
            )
        if result.is_error:
            from common.tools import ToolFailure

            raise ToolFailure("MCP_BUSINESS_ERROR", result.text, retryable=False)
        return result.structured or result.text

    return handler


def _normalize_annotations(raw: dict[str, bool]) -> dict[str, bool]:
    """MCP 用 readOnlyHint/destructiveHint，本地用 read_only/destructive。

    客户端负责抹平这层差异 —— 又是"接口标准化"的一个具体落点。
    """
    return {
        "read_only": bool(raw.get("readOnlyHint")),
        "destructive": bool(raw.get("destructiveHint")),
        "idempotent": bool(raw.get("idempotentHint")),
        "open_world": bool(raw.get("openWorldHint")),
    }


# --------------------------------------------------------------------------
# 对照组：不用 MCP，把工具 schema 硬编码在客户端
# --------------------------------------------------------------------------

# 这是"Function Calling 的静态配置"（笔记 4.1：在会话上下文中静态配置可用工具列表）。
# 服务端一旦改了参数，这里必须跟着改代码 —— 两边不同步就是线上事故。
STATIC_HARDCODED_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "query_knowledge",
            "description": "在指定集合里检索答案。",
            "parameters": {
                "type": "object",
                "properties": {
                    "collection": {"type": "string", "description": "集合名"},
                    "question": {"type": "string", "description": "要查的问题"},
                    # 注意这里没有 top_k —— 它是后来服务端才加的参数，
                    # 硬编码的客户端不会知道。
                },
                "required": ["collection", "question"],
            },
        },
    }
]
