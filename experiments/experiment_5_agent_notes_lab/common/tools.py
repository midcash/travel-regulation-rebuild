"""工具抽象层：就是笔记里那个"框架负责执行工具"的框架。

这个文件把笔记 4.4 节（MCPServerTool 设计原则）和 17.2 节（结构化错误）落成了代码：
  - 入参设计：必填尽量少、能给默认值就给、用 enum 收敛可选项、约束写进 schema（validate）
  - 出参设计：结构化返回 + 一段 LLM 友好的纯文本（ToolResult.content / .structured）
  - 错误处理：双层错误。协议级（工具不存在 / 参数非法）走 ToolResult.protocol_error；
              业务级走 is_error=True + 结构化 error 字段
  - ToolAnnotations：read_only / destructive / idempotent / open_world，声明副作用
  - 粒度：工具本身单一职责，"先发现 -> 再了解 -> 最后查询"靠模型自己组合

关键约定：错误信息要告诉 LLM 下一步该怎么办，而不是丢一堆堆栈。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

ToolSchema = dict[str, Any]

# JSON Schema 类型 -> Python 类型，用于 validate_arguments
_TYPE_MAP: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list, tuple),
    "object": (dict,),
}


class ToolFailure(Exception):
    """工具执行失败。带上结构化字段，让上层能分类处理（可重试 / 不可重试）。"""

    def __init__(
        self,
        error_type: str,
        message: str,
        retryable: bool = False,
        failed_fields: list[str] | None = None,
        retry_after: float | None = None,
    ):
        super().__init__(message)
        self.error_type = error_type
        self.message = message
        self.retryable = retryable
        self.failed_fields = failed_fields or []
        self.retry_after = retry_after

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "error_type": self.error_type,
            "retryable": self.retryable,
            "message": self.message,
        }
        if self.failed_fields:
            payload["failed_fields"] = self.failed_fields
        if self.retry_after is not None:
            payload["retry_after_seconds"] = self.retry_after
        return payload


@dataclass
class ToolResult:
    """一次工具调用的结果。ok=False 时 error 一定有值。"""

    ok: bool
    content: str = ""
    structured: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] = field(default_factory=dict)
    protocol_error: dict[str, Any] = field(default_factory=dict)
    elapsed_ms: float = 0.0
    attempts: int = 1
    from_cache: bool = False

    def to_message(self, call_id: str, tool_name: str) -> dict[str, Any]:
        """转成塞回对话历史的 tool 消息。

        注意这里刻意只回一段自然语言 + 结构化数据，不回 Python 堆栈——
        笔记 4.4 说得很直白：前者 LLM 能自主修正重试，后者只能原样念给用户听。
        """
        if self.protocol_error:
            text = json.dumps(self.protocol_error, ensure_ascii=False)
        elif not self.ok:
            text = "工具执行失败：" + json.dumps(self.error, ensure_ascii=False)
        else:
            text = self.content
        return {"role": "tool", "tool_call_id": call_id, "name": tool_name, "content": text}


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., Any]
    annotations: dict[str, bool] = field(default_factory=dict)
    output_schema: dict[str, Any] | None = None

    def schema(self) -> ToolSchema:
        """转成发给模型的 function 定义（OpenAI tools 数组里的一项）。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def describe_annotations(self) -> str:
        if not self.annotations:
            return "（未标注）"
        labels = {
            "read_only": "只读",
            "destructive": "有破坏性",
            "idempotent": "幂等",
            "open_world": "与外部世界交互",
        }
        return "、".join(labels.get(k, k) for k, v in self.annotations.items() if v) or "（无副作用）"


def validate_arguments(schema: dict[str, Any], arguments: dict[str, Any]) -> tuple[dict[str, Any], list[str], list[str]]:
    """按 JSON Schema 做一遍轻量校验，顺便把默认值填上。

    返回 (校验后的参数, 缺失字段, 类型/取值错误的字段)。
    这是笔记 2.2 节第 6 条"调用前对参数进行验证"的最小实现，也是 17.2.2 里
    failed_fields 的来源。

    注意：即使 LLM 传了符合 schema 的参数，真实 Server 端仍然要做业务层校验
    （路径穿越、注入、权限），这里只覆盖 schema 层。
    """
    properties: dict[str, Any] = schema.get("properties", {})
    required: list[str] = list(schema.get("required", []))
    cleaned: dict[str, Any] = {}
    missing: list[str] = []
    bad: list[str] = []

    # 先把调用方没传、但有默认值的补上——这是"必填参数尽量少"的兑现方式
    for key, spec in properties.items():
        if key not in arguments and "default" in spec:
            cleaned[key] = spec["default"]

    for key, value in arguments.items():
        if key not in properties:
            bad.append(key)  # schema 里没声明的字段：不静默接受
            continue
        cleaned[key] = value

    for key in required:
        if key not in cleaned or cleaned[key] is None:
            missing.append(key)

    for key, value in list(cleaned.items()):
        if key in missing:
            continue
        spec = properties.get(key, {})
        expected = spec.get("type")
        if expected and expected in _TYPE_MAP:
            # bool 是 int 的子类，这里要排掉，否则 True 会被当成合法整数
            if expected in {"integer", "number"} and isinstance(value, bool):
                bad.append(key)
                continue
            if not isinstance(value, _TYPE_MAP[expected]):
                bad.append(key)
                continue
        if "enum" in spec and value not in spec["enum"]:
            bad.append(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if "minimum" in spec and value < spec["minimum"]:
                bad.append(key)
            if "maximum" in spec and value > spec["maximum"]:
                bad.append(key)

    return cleaned, missing, bad


class ToolRegistry:
    """工具的注册表与执行器。模型只看得到 schema()，执行发生在这里。"""

    def __init__(self, name: str = "registry"):
        self.name = name
        self._tools: dict[str, Tool] = {}
        self.call_log: list[dict[str, Any]] = []

    def register(self, tool: Tool) -> Tool:
        if tool.name in self._tools:
            raise ValueError(f"工具重名: {tool.name}")
        self._tools[tool.name] = tool
        return tool

    def add(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        handler: Callable[..., Any],
        annotations: dict[str, bool] | None = None,
        output_schema: dict[str, Any] | None = None,
    ) -> Tool:
        return self.register(
            Tool(name, description, parameters, handler, annotations or {}, output_schema)
        )

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def schemas(self) -> list[ToolSchema]:
        """动态发现的来源：MCP 里对应 tools/list。"""
        return [tool.schema() for tool in self._tools.values()]

    def call(self, name: str, arguments: dict[str, Any], *, attempt: int = 1) -> ToolResult:
        """执行一次工具调用。所有异常都在这里被收敛成 ToolResult，不外泄。"""
        started = time.perf_counter()
        tool = self._tools.get(name)

        # ---- 第一层：协议级错误（JSON-RPC error code 那一层）----
        if tool is None:
            result = ToolResult(
                ok=False,
                protocol_error={
                    "code": -32601,
                    "error_type": "TOOL_NOT_FOUND",
                    "message": f"没有名为 {name!r} 的工具",
                    "available_tools": self.names(),
                },
                attempts=attempt,
            )
            self._log(name, arguments, result, started)
            return result

        cleaned, missing, bad = validate_arguments(tool.parameters, arguments or {})
        if missing or bad:
            detail = []
            if missing:
                detail.append(f"缺少必填参数 {missing}")
            if bad:
                detail.append(f"参数非法（缺失/类型不对/超范围）{bad}")
            result = ToolResult(
                ok=False,
                protocol_error={
                    "code": -32602,
                    "error_type": "INVALID_ARGUMENT",
                    "message": "；".join(detail),
                    "failed_fields": missing + bad,
                    "expected_schema": tool.parameters,
                },
                attempts=attempt,
            )
            self._log(name, arguments, result, started)
            return result

        # ---- 第二层：业务级错误（isError: true）----
        try:
            raw = tool.handler(**cleaned)
        except ToolFailure as exc:
            result = ToolResult(ok=False, error=exc.to_dict(), attempts=attempt)
        except Exception as exc:  # noqa: BLE001 - demo 里要把任何异常都收敛掉
            result = ToolResult(
                ok=False,
                error={
                    "error_type": "INTERNAL_ERROR",
                    "retryable": True,
                    "message": f"{type(exc).__name__}: {exc}",
                },
                attempts=attempt,
            )
        else:
            content, structured = _shape_output(raw)
            result = ToolResult(ok=True, content=content, structured=structured, attempts=attempt)

        self._log(name, arguments, result, started)
        return result

    def _log(self, name: str, arguments: dict[str, Any], result: ToolResult, started: float) -> None:
        result.elapsed_ms = (time.perf_counter() - started) * 1000
        self.call_log.append(
            {
                "tool": name,
                "arguments": arguments,
                "ok": result.ok,
                "error_type": result.error.get("error_type")
                or result.protocol_error.get("error_type"),
                "attempts": result.attempts,
                "elapsed_ms": round(result.elapsed_ms, 1),
            }
        )


def _shape_output(raw: Any) -> tuple[str, dict[str, Any]]:
    """统一出参形状：一段给 LLM 读的自然语言 + 一份结构化数据。

    笔记 4.4 说 MCP 规范要求"结构化返回的同时附带一个纯文本版本，保证老 Client 也能读"，
    这里就是那条要求的落地。LLM 读自然语言比读嵌套 JSON 效率高得多。
    """
    if isinstance(raw, str):
        return raw, {}
    if isinstance(raw, dict):
        return _humanize(raw), raw
    if isinstance(raw, (list, tuple)):
        return _humanize({"items": list(raw)}), {"items": list(raw)}
    return str(raw), {}


def _humanize(data: dict[str, Any], indent: int = 0) -> str:
    """把 dict 摊成缩进文本，比一坨 JSON 好读。"""
    lines: list[str] = []
    pad = "  " * indent
    for key, value in data.items():
        if isinstance(value, dict):
            lines.append(f"{pad}{key}:")
            lines.append(_humanize(value, indent + 1))
        elif isinstance(value, (list, tuple)):
            if value and isinstance(value[0], dict):
                lines.append(f"{pad}{key}:")
                for item in value:
                    lines.append(_humanize(item, indent + 1).rstrip())
            else:
                lines.append(f"{pad}{key}: {', '.join(str(v) for v in value)}")
        else:
            lines.append(f"{pad}{key}: {value}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 造几个假工具，让实验有东西可调
# --------------------------------------------------------------------------


def make_search_tool(corpus: dict[str, str]) -> Tool:
    """一个只读的检索工具。"""

    def handler(query: str, top_k: int = 2) -> dict[str, Any]:
        hits = [{"title": k, "snippet": v} for k, v in corpus.items() if query in k or query in v]
        hits = hits[:top_k]
        if not hits:
            # 业务级错误：告诉 LLM 下一步怎么办，而不是说"没找到"
            raise ToolFailure(
                "NO_RESULTS",
                f"没有匹配 {query!r} 的结果，可用的关键词有：{sorted(corpus)[:5]}",
                retryable=False,
            )
        return {"query": query, "count": len(hits), "results": hits}

    return Tool(
        name="search_knowledge",
        description="在内部知识库里检索。不确定有哪些条目时，先用 list_topics 查看。",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索关键词"},
                "top_k": {
                    "type": "integer",
                    "description": "返回条数",
                    "default": 2,
                    "minimum": 1,
                    "maximum": 5,
                },
            },
            "required": ["query"],
        },
        handler=handler,
        annotations={"read_only": True, "idempotent": True},
        output_schema={"type": "object", "properties": {"count": {"type": "integer"}}},
    )


def make_calculator_tool() -> Tool:
    """一个完全本地的计算器，用来演示"多步循环"里第二步依赖第一步结果。"""

    def handler(expression: str) -> dict[str, Any]:
        allowed = set("0123456789+-*/(). ")
        if not set(expression) <= allowed:
            raise ToolFailure(
                "INVALID_ARGUMENT",
                f"表达式含有不支持的字符，只允许数字和 + - * / ( )。收到的是 {expression!r}",
                retryable=False,
                failed_fields=["expression"],
            )
        try:
            # demo 用，只允许算术字符，不接 eval 的完整语法
            value = eval(compile(expression, "<calc>", "eval"), {"__builtins__": {}}, {})  # noqa: S307
        except ZeroDivisionError:
            raise ToolFailure("DIVISION_BY_ZERO", "除数为 0，请换一个表达式", retryable=False)
        return {"expression": expression, "value": value}

    return Tool(
        name="calculator",
        description="计算一个算术表达式，例如 (1+2)*3。需要精确数字时用它，不要心算。",
        parameters={
            "type": "object",
            "properties": {"expression": {"type": "string", "description": "算术表达式"}},
            "required": ["expression"],
        },
        handler=handler,
        annotations={"read_only": True, "idempotent": True},
    )


def make_hitl_tool(recorder: list[str]) -> Tool:
    """一个"转人工"工具，对应笔记里"保存状态并转人工"的那条降级路径。"""

    def handler(reason: str, priority: str = "normal") -> dict[str, Any]:
        recorder.append(reason)
        return {"handed_off": True, "ticket": f"HITL-{len(recorder):03d}", "reason": reason, "priority": priority}

    return Tool(
        name="escalate_to_human",
        description="把任务转交人工处理。自动路径都失败或涉及高风险操作时使用。",
        parameters={
            "type": "object",
            "properties": {
                "reason": {"type": "string", "description": "转人工的原因"},
                "priority": {"type": "string", "enum": ["low", "normal", "high"], "default": "normal"},
            },
            "required": ["reason"],
        },
        handler=handler,
        annotations={"open_world": True},
    )
