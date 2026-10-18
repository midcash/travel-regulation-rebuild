"""带"验证 → 反馈 → 重试"的函数调用器。对应笔记 2.2 第 6 条。

这条建议是六条里唯一一条"代码能救"的：schema 没写好，靠重试救不回来；
但 schema 写好了、模型只是偶尔手抖（漏了必填、传了越界值），
把结构化错误原样喂回去，模型就能定点修复。

关键设计（对应笔记 17.2.2）：
    反馈给模型的不是 Python 异常堆栈，而是
    {"error_type": ..., "retryable": ..., "message": ..., "failed_fields": [...]}。
    堆栈只能被原样念给用户听；结构化错误才能让模型自己改对。

重试是有界的：笔记 13.1.4 说"到达次数预算后立即停止，避免无限重试演变为重试风暴"。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from common.llm import ToolCall
from common.tools import ToolRegistry, ToolResult


@dataclass
class Attempt:
    index: int
    tool_call: ToolCall
    result: ToolResult

    @property
    def error_type(self) -> str:
        return (
            self.result.error.get("error_type")
            or self.result.protocol_error.get("error_type")
            or ""
        )


@dataclass
class CallOutcome:
    ok: bool
    attempts: list[Attempt] = field(default_factory=list)
    stopped_reason: str = ""  # success / unfixable / attempts_exhausted
    final: ToolResult | None = None

    @property
    def retries(self) -> int:
        return max(0, len(self.attempts) - 1)


class FunctionCaller:
    """模型负责构造参数，工具注册表负责校验和执行，这里负责把两者接起来。"""

    def __init__(self, model: Any, registry: ToolRegistry, max_attempts: int = 3):
        self.model = model
        self.registry = registry
        self.max_attempts = max_attempts
        self.outcomes: list[CallOutcome] = []

    def call(self, schema: dict[str, Any], *, on_attempt=None) -> CallOutcome:
        feedback: dict[str, Any] | None = None
        outcome = CallOutcome(ok=False)

        for index in range(1, self.max_attempts + 1):
            tool_call = self.model.construct(schema, feedback)
            result = self.registry.call(tool_call.name, tool_call.arguments, attempt=index)
            attempt = Attempt(index, tool_call, result)
            outcome.attempts.append(attempt)
            if on_attempt:
                on_attempt(attempt)

            if result.ok:
                outcome.ok = True
                outcome.stopped_reason = "success"
                outcome.final = result
                break

            # ---- 判断这次失败能不能修 ----
            if result.protocol_error:
                code = result.protocol_error.get("code")
                if code == -32601:  # 工具名都不对，重试没有意义
                    outcome.stopped_reason = "unfixable"
                    outcome.final = result
                    break
                # -32602 参数非法：带上 failed_fields 和 expected_schema 回去让模型改
                feedback = result.protocol_error
            else:
                error = result.error
                if not error.get("failed_fields"):
                    # 没有任何"该改哪个字段"的线索，模型无从下手，重试只是白花钱
                    outcome.stopped_reason = "unfixable"
                    outcome.final = result
                    break
                feedback = error
        else:
            outcome.stopped_reason = "attempts_exhausted"
            outcome.final = outcome.attempts[-1].result

        self.outcomes.append(outcome)
        return outcome
