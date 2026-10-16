"""工具调用失败的四步处理流程。对应笔记 17.2 节。

笔记把工具故障分成四类，处理方式完全不同 —— 这是这一节最容易被面试官追问的地方：

    1. 瞬时技术故障（网络中断、超时、429、5xx、MCP Server 短暂不可用）
       -> 允许重试，但必须有界：超时 + 总预算 + 指数退避 + 随机抖动
    2. 确定性调用错误（参数缺失/类型错、Schema 校验失败、权限不足、违反业务规则）
       -> 原样重试没有意义。返回结构化错误，让模型修正参数/重新授权，修不了就终止
    3. 瞬时故障多次重试仍失败
       -> 熔断器隔离故障：Closed -> Open -> Half-Open，保护 Agent 和下游
    4. 工具长时间不可用
       -> 按能力等级逐级降级：备用工具 -> 缓存 -> 部分结果 -> 只读建议 -> 转人工 -> 明确失败

一句话概括（笔记原话）：先分类，能恢复就受控恢复；不能恢复就熔断；确实不可用就优雅降级。
最后那条底线是：**绝不能伪造成功结果**。

时间在这里是可注入的（Clock），这样 demo 不用真的等 8 秒，
在调试器里也能一次跑完看到全部状态迁移。
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Protocol

from common.tools import ToolRegistry, ToolResult


# --------------------------------------------------------------------------
# 时间：可注入，让退避等待在 demo 里瞬间走完
# --------------------------------------------------------------------------


class Clock(Protocol):
    def now(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...


class RealClock:
    def __init__(self) -> None:
        import time

        self._time = time
        self._start = time.monotonic()
        self.slept = 0.0

    def now(self) -> float:
        return self._time.monotonic() - self._start

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            self.slept += seconds
            self._time.sleep(seconds)


class VirtualClock:
    """不真的睡，只把时间往前推，并记下"本来要等多久"。"""

    def __init__(self) -> None:
        self._now = 0.0
        self.slept = 0.0
        self.waits: list[float] = []

    def now(self) -> float:
        return self._now

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            self._now += seconds
            self.slept += seconds
            self.waits.append(round(seconds, 3))


# --------------------------------------------------------------------------
# 一、有界重试：指数退避 + 随机抖动
# --------------------------------------------------------------------------


@dataclass
class RetryPolicy:
    """笔记 17.2.1 的三个约束：单次超时、总预算、最大重试次数。"""

    max_attempts: int = 3
    base_delay: float = 0.5
    max_delay: float = 8.0
    jitter_ratio: float = 0.3  # 随机抖动：把不同客户端的重试时刻错开
    total_budget_s: float = 15.0  # 整个工具调用阶段的时间预算
    timeout_s: float = 5.0  # 单次调用的响应超时

    def __post_init__(self) -> None:
        self._rng = random.Random(20260911)  # 固定种子，demo 可复现

    def delay_for(self, attempt: int, retry_after: float | None = None) -> float:
        """第 attempt 次失败后该等多久。attempt 从 1 开始。

        指数退避：0.5s -> 1s -> 2s -> 4s ...（上限 max_delay）
        随机抖动：在结果上乘一个 [1-j, 1+j] 的系数
        遵守 Retry-After：服务端明确要求等多久时听它的（取两者较大值）
        """
        backoff = min(self.max_delay, self.base_delay * (2 ** (attempt - 1)))
        jittered = backoff * (1 + self._rng.uniform(-self.jitter_ratio, self.jitter_ratio))
        delay = max(0.0, jittered)
        if retry_after is not None:
            delay = max(delay, retry_after)
        return min(delay, self.max_delay)


@dataclass
class Budget:
    """笔记反复强调的"预算就是架构的一部分"。"""

    max_attempts: int = 3
    max_seconds: float = 15.0
    attempts: int = 0
    spent_seconds: float = 0.0

    def remaining_attempts(self) -> int:
        return max(0, self.max_attempts - self.attempts)

    def can_retry(self) -> bool:
        return self.remaining_attempts() > 0 and self.spent_seconds < self.max_seconds

    def spend(self, seconds: float) -> None:
        self.attempts += 1
        self.spent_seconds += seconds


# --------------------------------------------------------------------------
# 二、熔断器：Closed -> Open -> Half-Open
# --------------------------------------------------------------------------


@dataclass
class CircuitBreaker:
    """笔记 17.2.3：熔断的目的不是修复工具，而是保护 Agent 和下游系统。"""

    name: str
    failure_threshold: int = 3
    cooldown_s: float = 10.0
    half_open_probes: int = 1
    state: str = "closed"
    consecutive_failures: int = 0
    opened_at: float = 0.0
    probes_left: int = 0
    clock: Clock | None = None
    history: list[str] = field(default_factory=list)

    def allow(self, clock: Clock) -> tuple[bool, str]:
        """这次调用放不放行。"""
        if self.state == "closed":
            return True, "closed：正常放行"

        if self.state == "open":
            elapsed = clock.now() - self.opened_at
            if elapsed < self.cooldown_s:
                return False, f"open：拒绝调用，冷却中（还剩 {self.cooldown_s - elapsed:.1f}s）"
            # 冷却结束，进入半开，只放行少量探测请求。
            # 这一次本身就是第一个探测名额，所以这里要一起扣掉 ——
            # 不然 half_open_probes=1 实际会放行 2 个请求，
            # "只放行 1 个探测"这句话就成了一句空话。
            self._transition("half_open")
            self.probes_left = self.half_open_probes - 1
            return True, "half_open：放行探测请求"

        # half_open
        if self.probes_left > 0:
            self.probes_left -= 1
            return True, "half_open：放行探测请求"
        return False, "half_open：探测名额已用完，等待结果"

    def record(self, ok: bool) -> None:
        if ok:
            self.consecutive_failures = 0
            if self.state == "half_open":
                # 探测成功 -> 依赖恢复了，关闭熔断
                self._transition("closed")
            return

        self.consecutive_failures += 1
        if self.state == "half_open":
            # 探测失败 -> 依赖还没恢复，重新打开，再等一个冷却周期
            self.opened_at = self._now()
            self._transition("open")
            return
        if self.consecutive_failures >= self.failure_threshold:
            self.opened_at = self._now()
            self._transition("open")

    def trip_open(self) -> None:
        """人工/上层强制打开熔断。"""
        self.opened_at = self._now()
        self._transition("open")

    def _now(self) -> float:
        return self.clock.now() if self.clock else 0.0

    def _transition(self, new_state: str) -> None:
        self.state = new_state
        self.history.append(new_state)


# --------------------------------------------------------------------------
# 三、错误分类
# --------------------------------------------------------------------------

TRANSIENT = "transient"
DETERMINISTIC = "deterministic"


def classify(result: ToolResult) -> tuple[str, str]:
    """先分类。笔记原话：不能对所有失败统一重试。

    返回 (类别, 原因说明)。
    """
    if result.ok:
        return "", "成功"

    if result.protocol_error:
        code = result.protocol_error.get("code")
        if code == -32601:
            return DETERMINISTIC, "工具不存在（协议级 -32601），重试没有意义"
        if code == -32602:
            return DETERMINISTIC, "参数非法（协议级 -32602），原样重试没有意义"
        return DETERMINISTIC, f"协议级错误 code={code}"

    error = result.error
    if error.get("retryable"):
        return TRANSIENT, f"{error.get('error_type')}：过一段时间可能自行恢复"
    return DETERMINISTIC, f"{error.get('error_type')}：由请求本身或执行条件决定，不会随时间消失"


# --------------------------------------------------------------------------
# 四、完整的四步流程
# --------------------------------------------------------------------------


@dataclass
class ExecOutcome:
    ok: bool
    action: str
    detail: str
    result: ToolResult | None = None
    attempts: int = 0
    trace: list[str] = field(default_factory=list)
    degraded: bool = False
    missing: list[str] = field(default_factory=list)

    def say(self, line: str) -> None:
        self.trace.append(line)


@dataclass
class Fallback:
    """降级路径的一环。按顺序尝试。"""

    kind: str  # fallback_tool / cache / partial
    label: str
    call: Any  # () -> ToolResult | dict


class ToolExecutor:
    """把笔记 17.2.5 那张流程图变成代码。"""

    def __init__(
        self,
        registry: ToolRegistry,
        retry: RetryPolicy | None = None,
        breaker: CircuitBreaker | None = None,
        clock: Clock | None = None,
    ):
        self.registry = registry
        self.retry = retry or RetryPolicy()
        self.clock = clock or VirtualClock()
        self.breaker = breaker or CircuitBreaker("default")
        # 熔断器需要时钟来判断冷却是否结束；这里统一注入，避免两处时间来源不一致
        self.breaker.clock = self.clock

    def call(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        fallbacks: list[Fallback] | None = None,
        escalate: Any = None,
        allow_degradation: bool = True,
    ) -> ExecOutcome:
        outcome = ExecOutcome(ok=False, action="", detail="")

        # ---- 第 1 步：熔断检查（在打网络之前）----
        allowed, why = self.breaker.allow(self.clock)
        outcome.say(f"[熔断器] {why}")
        if not allowed:
            outcome.action = "circuit_open"
            outcome.detail = "熔断器打开，直接拒绝调用，没有冲击下游"
            return self._degrade_or_fail(outcome, name, fallbacks, escalate, allow_degradation)

        # ---- 第 2 步：带预算的循环 ----
        budget = Budget(max_attempts=self.retry.max_attempts, max_seconds=self.retry.total_budget_s)
        last: ToolResult | None = None

        def settle(out: ExecOutcome) -> ExecOutcome:
            """一次**调用**的最终结果，记一次熔断器的成绩。

            注意粒度：不是"每次重试记一次"，而是"每次调用记一次"。

            熔断器要隔离的是「这个下游连续多次调用都不行」，所以它统计的
            单位必须是**调用**。按尝试记会有一个很隐蔽的后果：阈值 3 的含义
            悄悄变成了「一次调用里重试 3 次」—— 熔断器于是变成了重试次数的
            复读机，而真正该被拦住的「反复调用反复失败」反而看不出来。
            """
            self.breaker.record(bool(last and last.ok))
            return out

        while True:
            started = self.clock.now()
            result = self._invoke(name, arguments, budget.attempts + 1)
            budget.spend(self.clock.now() - started)
            last = result

            if result.ok:
                outcome.ok = True
                outcome.action = "success" if budget.attempts == 1 else "retried_success"
                outcome.detail = (
                    f"第 {budget.attempts} 次调用成功"
                    if budget.attempts > 1
                    else "一次调用成功"
                )
                outcome.result = result
                outcome.attempts = budget.attempts
                outcome.say(f"[调用] 第 {budget.attempts} 次 -> 成功")
                return settle(outcome)

            category, reason = classify(result)
            outcome.say(f"[分类] 第 {budget.attempts} 次失败 -> {category}：{reason}")

            # 确定性错误：原样重试没有意义
            if category == DETERMINISTIC:
                outcome.ok = False
                outcome.result = result
                outcome.attempts = budget.attempts
                payload = result.protocol_error or result.error
                if payload.get("failed_fields"):
                    outcome.action = "hand_back_to_caller"
                    outcome.detail = (
                        "确定性错误，不做重试；把结构化错误交回调用方（模型）去修正参数、"
                        "重新授权或终止"
                    )
                    outcome.say(
                        f"[停止] failed_fields={payload['failed_fields']} —— 交回模型去修，而不是重复同一个请求"
                    )
                else:
                    outcome.action = "terminate"
                    outcome.detail = "确定性错误且没有可修正的字段，直接终止调用并说明原因"
                    outcome.say("[停止] 无可修正字段，终止")
                return settle(outcome)

            # 瞬时故障：判断预算还够不够
            if not budget.can_retry():
                outcome.ok = False
                outcome.result = result
                outcome.attempts = budget.attempts
                outcome.say(
                    f"[停止] 预算用尽（已调用 {budget.attempts} 次，"
                    f"耗时 {budget.spent_seconds:.1f}s / {budget.max_seconds}s）"
                )
                if self.breaker.state == "open":
                    outcome.say("[熔断器] 进入 open 状态，暂时拒绝新调用")
                outcome.action = "retries_exhausted"
                outcome.detail = "瞬时故障重试到预算上限仍未恢复"
                return settle(
                    self._degrade_or_fail(
                        outcome, name, fallbacks, escalate, allow_degradation, keep_action=True
                    )
                )

            retry_after = (last.error or {}).get("retry_after_seconds")
            delay = self.retry.delay_for(budget.attempts, retry_after)
            if budget.spent_seconds + delay > budget.max_seconds:
                outcome.say(f"[停止] 下一次退避 {delay:.2f}s 会超出总预算，不再重试")
                outcome.ok = False
                outcome.result = result
                outcome.attempts = budget.attempts
                outcome.action = "budget_exhausted"
                outcome.detail = "继续退避会超出总时间预算"
                return settle(
                    self._degrade_or_fail(
                        outcome, name, fallbacks, escalate, allow_degradation, keep_action=True
                    )
                )
            outcome.say(f"[退避] 等待 {delay:.2f}s 后重试（指数退避 + 随机抖动）")
            self.clock.sleep(delay)
            # 退避等待也算在"整个工具调用阶段的总时间预算"里 ——
            # 否则重试风暴会把预算耗尽的任务拖成无限等待
            budget.spent_seconds += delay

    def _invoke(self, name: str, arguments: dict[str, Any], attempt: int) -> ToolResult:
        """执行一次工具调用，并在**这里**执行单次调用的响应超时。

        真实系统里超时是传输层的事（socket timeout、asyncio.wait_for、
        MCP 客户端的 request timeout）。这里没有网络，就用注入的时钟做
        同一件事：调用返回后量一下耗时，超过 RetryPolicy.timeout_s 的结果
        直接判为超时 —— **哪怕它其实算出来了**。

        这一点值得记住，也是 timeout_s 存在的意义：

            超时是**调用方**对延迟的容忍度，不是工具的错误。
            工具可以干得好好的，只是太慢了；对调用方来说，
            一个迟到 8 秒的正确结果和一个失败没有区别。

        所以它被归到 TIMEOUT（retryable=True），走瞬时故障那条路：
        重试 / 熔断 / 降级，而不是当成确定性错误直接终止。
        """
        started = self.clock.now()
        result = self.registry.call(name, arguments, attempt=attempt)
        elapsed = self.clock.now() - started

        if result.ok and elapsed > self.retry.timeout_s:
            return ToolResult(
                ok=False,
                error={
                    "error_type": "TIMEOUT",
                    "retryable": True,
                    "message": (
                        f"单次调用超过响应超时 "
                        f"（{elapsed:.1f}s > timeout_s={self.retry.timeout_s:.1f}s），结果作废"
                    ),
                },
                elapsed_ms=result.elapsed_ms,
                attempts=attempt,
            )
        return result

    # -- 第 4 步：优雅降级 --------------------------------------------------

    def _degrade_or_fail(
        self,
        outcome: ExecOutcome,
        name: str,
        fallbacks: list[Fallback] | None,
        escalate: Any,
        allow_degradation: bool,
        keep_action: bool = False,
    ) -> ExecOutcome:
        outcome.say("[降级] 尝试按能力等级逐级降级")

        # allow_degradation=False 关掉的是**降级链**，不是"转人工"。
        #
        # 这两件事经常被混成一件，但它们的后果完全相反：
        #   降级   —— 换一个更弱的能力顶上（缓存 / 部分结果），任务继续，代价是数据可能不对；
        #   转人工 —— 承认自动路径做不了，把决定权交回人。
        #
        # 高风险写操作（退款、删除）恰恰**最需要**转人工：
        # 它不能接受降级，但它更不能接受"悄悄失败"——
        # 用户的钱卡在半路上，必须有人接手。以前这两个开关是同一个，
        # 于是"不允许降级"顺带把转人工也关了，操作直接掉进 failed_explicitly。
        ladder = [] if not allow_degradation else list(fallbacks or [])
        if not allow_degradation:
            outcome.say("[降级] 该操作不允许降级（例如高风险写操作），不尝试任何替代能力")
            outcome.say("[降级] 但「不允许降级」≠「不允许转人工」—— 仍会尝试转人工")

        for fallback in ladder:
            try:
                raw = fallback.call()
            except Exception as exc:  # noqa: BLE001
                outcome.say(f"[降级] {fallback.label} 也不可用：{exc}")
                continue

            if isinstance(raw, ToolResult):
                if raw.ok:
                    outcome.ok = True
                    outcome.result = raw
                    outcome.degraded = True
                    outcome.action = f"degraded_{fallback.kind}"
                    outcome.detail = f"降级到{fallback.label}，任务继续"
                    outcome.say(f"[降级] 命中{fallback.label}，任务可以继续（但能力有损）")
                    return outcome
                outcome.say(f"[降级] {fallback.label} 返回失败")
                continue

            if isinstance(raw, dict) and raw:
                payload = dict(raw)
                # __missing__ 是降级结果里的"明确缺失项"约定，不进结构化数据
                missing = list(payload.pop("__missing__", []))
                outcome.ok = True
                outcome.degraded = True
                outcome.action = f"degraded_{fallback.kind}"
                outcome.missing = missing
                outcome.detail = f"降级到{fallback.label}"
                outcome.say(f"[降级] 命中{fallback.label}")
                if missing:
                    outcome.say(f"[降级] 必须向用户明确缺失项：{missing}")
                outcome.result = ToolResult(
                    ok=True,
                    content="；".join(f"{k}={v}" for k, v in payload.items()),
                    structured=payload,
                )
                return outcome

        # 没有安全降级路径
        if escalate is not None:
            try:
                ticket = escalate(f"{name} 长时间不可用，且没有安全的降级路径")
                outcome.action = "escalated_to_human"
                outcome.detail = f"保存状态并转人工：{ticket}"
                outcome.say(f"[降级] 没有安全降级路径 -> 保存状态、转人工（{ticket}）")
                return outcome
            except Exception as exc:  # noqa: BLE001
                outcome.say(f"[降级] 转人工也失败了：{exc}")

        outcome.action = "failed_explicitly"
        outcome.detail = "没有任何安全路径 -> 明确失败，绝不伪造成功结果"
        outcome.say("[降级] 没有任何安全路径 -> 明确失败（笔记底线：绝不能伪造成功结果）")
        return outcome
