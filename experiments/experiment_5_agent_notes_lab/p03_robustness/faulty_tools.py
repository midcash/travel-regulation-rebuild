"""会按剧本出故障的假工具。

笔记 17.2 把工具故障分成四类。要演示这四类的处理差异，就必须能**稳定复现**
每一类故障——真实网络抖动是随机的，在调试器里根本没法看。
所以这里用"第几次调用抛什么异常"的剧本把故障确定下来。

剧本 plan 是一个列表，第 i 项对应第 i+1 次调用：
    None                  -> 这次调用成功
    ToolFailure(...)      -> 这次调用抛出这个故障
列表用完后，一直重复最后一项（方便演示"持续故障"）。
"""
from __future__ import annotations

from typing import Any, Callable

from common.tools import Tool, ToolFailure

# ---- 四类故障的样本 ------------------------------------------------------

def timeout_fault() -> ToolFailure:
    """瞬时技术故障：请求超时。"""
    return ToolFailure("TIMEOUT", "读取超时：下游 5s 内没有响应", retryable=True)


def rate_limited_fault(retry_after: float | None = 1.0) -> ToolFailure:
    """瞬时技术故障：HTTP 429 限流，并且服务端给了 Retry-After。"""
    return ToolFailure(
        "RATE_LIMITED",
        "HTTP 429：请求过于频繁",
        retryable=True,
        retry_after=retry_after,
    )


def server_error_fault() -> ToolFailure:
    """瞬时技术故障：HTTP 503。"""
    return ToolFailure("SERVER_ERROR", "HTTP 503：服务暂时不可用", retryable=True)


def permission_fault() -> ToolFailure:
    """确定性错误：权限不足。重试一百次也还是没权限。"""
    return ToolFailure(
        "PERMISSION_DENIED",
        "当前凭证没有查询该航司报价的权限，需要重新授权",
        retryable=False,
    )


def business_rule_fault() -> ToolFailure:
    """确定性错误：违反业务规则。"""
    return ToolFailure(
        "BUSINESS_RULE_VIOLATION",
        "该航班不支持改签，只能退票",
        retryable=False,
        failed_fields=["flight_no"],
    )


def make_scripted_tool(
    name: str,
    description: str,
    plan: list[ToolFailure | None],
    payload: dict[str, Any],
    parameters: dict[str, Any] | None = None,
    annotations: dict[str, bool] | None = None,
    clock: Any = None,
    delay_s: float = 0.0,
) -> tuple[Tool, dict[str, int]]:
    """按剧本出故障的工具。返回 (工具, 调用计数器)，计数器方便 demo 里展示"打了几次下游"。

    传了 clock + delay_s，这个工具就会**消耗时间**：每次调用占用 delay_s 秒，
    用来演示单次调用的响应超时（RetryPolicy.timeout_s）。不传就是瞬时返回，
    和以前完全一样（演示 1~7 里的其他工具都不传）。
    """
    counter = {"calls": 0}

    def handler(**kwargs: Any) -> dict[str, Any]:
        index = counter["calls"]  # 第几次调用（从 0 开始）
        counter["calls"] += 1
        if clock is not None and delay_s > 0:
            # 请求已经发出去了，时间就花掉了 —— 无论最后是成功还是超时，
            # 下游都实实在在占用了这么久。这一点很重要：
            # 超时的调用同样消耗预算，所以"重试到预算耗尽"是真实会发生的。
            clock.sleep(delay_s)
        fault = plan[index] if index < len(plan) else plan[-1]
        if fault is not None:
            raise fault
        return dict(payload)

    tool = Tool(
        name=name,
        description=description,
        parameters=parameters or {"type": "object", "properties": {}, "required": []},
        handler=handler,
        annotations=annotations or {"read_only": True, "idempotent": True},
    )
    return tool, counter


def make_cache_lookup(payloads: dict[str, dict[str, Any]]) -> Callable[[str], dict[str, Any] | None]:
    """一个"缓存/已有中间结果"的查询口子，用于降级链里的一环。"""
    return lambda key: payloads.get(key)
