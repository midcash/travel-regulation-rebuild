"""原型 03：Agent 健壮性设计。对应笔记第 17 章（工具故障 + 模型故障）。

这一章是笔记里最工程化的一章，也是最容易在面试里被追问的一章
（英伟达一面：Agent 遇到异常故障时，用什么方式保持持续健壮的运行？
  宇树一面：外部工具调用失败后，系统如何容错或降级？）

笔记给的答案是一套四步流程，这个原型把它变成了可以单步跟踪的代码：

    先分类 -> 能恢复就受控恢复 -> 不能恢复就熔断 -> 确实不可用就优雅降级
    底线是：绝不能伪造成功结果。

运行方式（PyCharm 直接右键 Run / Debug）：
    python experiments/experiment_5_agent_notes_lab/p03_robustness/main.py

时间是可注入的（VirtualClock）：退避、冷却、半开探测全都瞬间走完，
但每一步"本来要等多久"都打印出来了，调试时不用真的等。

建议打断点的位置：
  - resilience.py 的 `category, reason = classify(result)`：故障分类的分水岭
  - resilience.py 的 `allowed, why = self.breaker.allow(self.clock)`：熔断状态机（一次**调用**问一次）
  - resilience.py 的 `settle(...)`：一次调用的最终结果记一次熔断成绩（不是每次重试都记）
  - resilience.py 的 `_invoke(...)`：单次响应超时在这里执行 —— 迟到 = 失败
  - resilience.py 的 `_degrade_or_fail(...)`：降级链是一级一级试的，转人工是它的下一站
  - model_gateway.py 的 `permission = downgrade(permission, 1)`：权限怎么跟着能力降
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from common import trace  # noqa: E402
from common.llm import LLMResponse, MockLLM, ScriptedLLM, ToolCall  # noqa: E402
from common.tools import ToolRegistry, make_hitl_tool  # noqa: E402
from faulty_tools import (  # noqa: E402
    business_rule_fault,
    make_scripted_tool,
    permission_fault,
    rate_limited_fault,
    timeout_fault,
)
from model_gateway import (  # noqa: E402
    ModelGateway,
    ModelSpec,
    Permission,
    QualityMonitor,
    pass_k,
    require_permission,
)
from resilience import (  # noqa: E402
    CircuitBreaker,
    ExecOutcome,
    Fallback,
    RetryPolicy,
    ToolExecutor,
    VirtualClock,
)


def registry_with(*tools) -> ToolRegistry:
    registry = ToolRegistry("p03")
    for tool in tools:
        registry.register(tool)
    return registry


def show_outcome(outcome: ExecOutcome, counter: dict[str, int] | None = None) -> None:
    print()
    for line in outcome.trace:
        print(f"    {line}")
    trace.rule("-")
    trace.kv("最终动作", outcome.action)
    trace.kv("说明", outcome.detail)
    trace.kv("实际尝试次数", outcome.attempts)
    if counter is not None:
        trace.kv("下游被真实调用的次数", counter["calls"])
    if outcome.missing:
        trace.kv("明确缺失项", outcome.missing)


# ==========================================================================
# 演示 1：瞬时故障 -> 有界重试
# ==========================================================================


def demo_1_transient_retry() -> None:
    trace.section(
        "演示 1：瞬时技术故障 —— 超时后受控重试，第三次成功",
        "笔记 17.2.1：设置明确的超时、总预算和有界重试（指数退避 + 随机抖动）",
    )
    tool, counter = make_scripted_tool(
        "search_flights",
        "查询航班报价",
        plan=[timeout_fault(), timeout_fault(), None],
        payload={"flight": "CA1234", "price": 1280},
    )
    clock = VirtualClock()
    executor = ToolExecutor(registry_with(tool), retry=RetryPolicy(max_attempts=4), clock=clock)
    outcome = executor.call("search_flights", {})
    show_outcome(outcome, counter)
    trace.note(f"退避序列（虚拟时间）：{clock.waits} 秒 —— 指数退避把重试间隔逐步拉长")
    trace.note("随机抖动的作用：把不同客户端的重试时刻错开，避免重试风暴同时砸向下游")


# ==========================================================================
# 演示 1-b：单次响应超时（RetryPolicy.timeout_s）
# ==========================================================================


def demo_1b_response_timeout() -> None:
    trace.section(
        "演示 1-b：工具其实算出来了，但超过了响应超时 —— 结果作废",
        "笔记 17.2.1 的第一个约束：设置明确的单次超时",
    )
    clock = VirtualClock()
    tool, counter = make_scripted_tool(
        "search_flights_slow",
        "查询航班报价（每次都要 8 秒才回）",
        plan=[None],  # 剧本里**没有故障**：每次都"成功"，只是慢
        payload={"flight": "CA1234", "price": 1280},
        clock=clock,
        delay_s=8.0,
    )
    executor = ToolExecutor(
        registry_with(tool),
        # timeout_s=5，而工具每次都花 8 秒 —— 这条线才是演示的主角
        retry=RetryPolicy(max_attempts=2, timeout_s=5.0, total_budget_s=30.0),
        clock=clock,
    )
    outcome = executor.call("search_flights_slow", {})
    show_outcome(outcome, counter)

    trace.note("注意剧本：这个工具**从没抛过异常**，它每次都把结果算出来了。")
    trace.note("但它花了 8 秒，超过了 timeout_s=5 秒 —— 于是结果被判为作废。")
    trace.note("")
    trace.note("这一条值得反复看：超时是**调用方**对延迟的容忍度，不是工具的错误。")
    trace.note("一个迟到 8 秒的正确结果，和一个失败，对调用方没有区别。")
    trace.note("所以它归到 TIMEOUT（retryable=True），走瞬时故障那条路 ——")
    trace.note("重试、熔断、降级都适用它，而不是当成确定性错误直接终止。")
    trace.note("")
    trace.note("也正因为超时的调用**同样消耗预算**，单次超时和总预算必须是两个约束：")
    trace.note("每次调用都「刚刚好」卡在超时线以内，重试 10 次照样能把整个任务拖垮。")


# ==========================================================================
# 演示 2：确定性错误 -> 一次都不重试
# ==========================================================================


def demo_2_deterministic() -> None:
    trace.section(
        "演示 2：确定性调用错误 —— 一次都不重试",
        "笔记 17.2.2：这类错误由请求本身决定，不会随时间消失，原样重试没有意义",
    )

    for label, plan, note in [
        (
            "2-a 违反业务规则（带 failed_fields，交回模型去修）",
            [business_rule_fault()],
            "错误信息里有 failed_fields，模型知道该改哪个字段",
        ),
        (
            "2-b 权限不足（没有可修正字段，直接终止）",
            [permission_fault()],
            "重新授权是人的事，重试一百次也还是没权限",
        ),
    ]:
        print()
        trace.section(label, note)
        tool, counter = make_scripted_tool(
            "change_flight", "改签航班", plan=plan, payload={"ok": True}, annotations={"read_only": False}
        )
        executor = ToolExecutor(registry_with(tool), retry=RetryPolicy(max_attempts=3), clock=VirtualClock())
        outcome = executor.call("change_flight", {})
        show_outcome(outcome, counter)
    trace.note("对比演示 1：同样是失败，瞬时故障试了 3 次，确定性错误只试了 1 次。")
    trace.note("这就是笔记那句「不能对所有失败统一重试」。")


# ==========================================================================
# 演示 3：持续故障 -> 熔断器的完整状态迁移
# ==========================================================================


def demo_3_circuit_breaker() -> None:
    trace.section(
        "演示 3：瞬时故障反复失败 —— 熔断器隔离故障",
        "笔记 17.2.3：Closed -> Open -> 冷却 -> Half-Open 探测 -> Closed",
    )
    tool, counter = make_scripted_tool(
        "search_hotels",
        "查询酒店",
        plan=[timeout_fault()] * 9 + [None],  # 连续 9 次超时，第 10 次下游才恢复
        payload={"hotel": "外滩某酒店", "price": 680},
    )
    clock = VirtualClock()
    breaker = CircuitBreaker("search_hotels", failure_threshold=3, cooldown_s=10.0)
    executor = ToolExecutor(
        registry_with(tool), retry=RetryPolicy(max_attempts=3), breaker=breaker, clock=clock
    )

    trace.note("熔断器的阈值数的是**调用**，不是尝试：连续 3 次调用都失败才熔断。")
    trace.note("下面每一轮「1 次调用」内部自己会重试 3 次 —— 那是重试预算的事，和熔断器无关。")
    trace.note("两个预算各管各的。混在一起数，熔断器就变成了重试次数的复读机。")

    for index in range(1, 4):
        print()
        trace.section(f"第 {index} 次调用（工具正在持续故障）")
        outcome = executor.call("search_hotels", {})
        show_outcome(outcome, counter)
        trace.kv("熔断器状态", breaker.state)
        trace.kv("连续失败的【调用】次数", breaker.consecutive_failures)

    print()
    trace.section("熔断已打开：再打一次，根本碰不到下游")
    before = counter["calls"]
    outcome = executor.call("search_hotels", {})
    show_outcome(outcome)
    trace.kv("下游被真实调用的次数", f"{before} -> {counter['calls']}（一次都没增加）")
    trace.note("这就是笔记说的「熔断的目的不是修复工具，而是保护 Agent 和下游系统」。")

    print()
    trace.section("冷却 10 秒后：半开状态放行探测请求")
    clock.sleep(10.0)
    outcome = executor.call("search_hotels", {})
    show_outcome(outcome, counter)
    trace.kv("熔断器状态", breaker.state)
    trace.kv("状态迁移历史", breaker.history)
    trace.note("closed -> open -> half_open -> closed，这就是一次完整的状态迁移。")


# ==========================================================================
# 演示 4：工具长时间不可用 -> 逐级降级
# ==========================================================================


def demo_4_degradation_ladder() -> None:
    trace.section(
        "演示 4：工具长时间不可用 —— 按能力等级逐级降级",
        "笔记 17.2.4：备用工具 -> 缓存/已有结果 -> 部分结果 -> 转人工",
    )

    escalated: list[str] = []

    def escalate(reason: str) -> str:
        escalated.append(reason)
        return f"HITL-{len(escalated):03d}"

    def run(label: str, fallbacks: list[Fallback], note: str) -> None:
        print()
        trace.section(label, note)
        tool, counter = make_scripted_tool(
            "search_flights", "查询航班报价", plan=[timeout_fault()], payload={}
        )
        executor = ToolExecutor(
            registry_with(tool), retry=RetryPolicy(max_attempts=2), clock=VirtualClock()
        )
        outcome = executor.call("search_flights", {}, fallbacks=fallbacks, escalate=escalate)
        show_outcome(outcome, counter)

    backup_tool, _ = make_scripted_tool(
        "search_flights_backup", "备用航班数据源", plan=[None], payload={"flight": "MU5678", "price": 1310}
    )
    backup_registry = registry_with(backup_tool)

    run(
        "4-a 第一级：切换到同能力的备用工具",
        [
            Fallback(
                "fallback_tool",
                "备用工具 search_flights_backup",
                lambda: backup_registry.call("search_flights_backup", {}),
            )
        ],
        "备用工具是事先准备好的同能力替代品",
    )

    cache = {
        "flight:CA1234": {"flight": "CA1234", "price": 1280, "cached_at": "2026-09-11 10:00"}
    }
    run(
        "4-b 第二级：备用工具也挂了 -> 用缓存/已有中间结果",
        [Fallback("cache", "缓存中的上一次查询结果", lambda: cache.get("flight:CA1234"))],
        "缓存数据可能过期，返回时必须说明这是缓存",
    )

    run(
        "4-c 第三级：缓存也没有 -> 返回部分结果，并明确缺失项",
        [
            Fallback("cache", "缓存", lambda: None),
            Fallback(
                "partial",
                "只返回航班号，不含实时价格",
                lambda: {"flight": "CA1234", "source": "历史订单", "__missing__": ["实时价格", "余票"]},
            ),
        ],
        "笔记要求：无法完整完成时返回部分结果且明确缺失项",
    )

    run(
        "4-d 第四级：什么都没有 -> 保存状态、转人工",
        [],
        "没有安全降级路径时，保存必要状态并转人工",
    )
    trace.kv("人工工单", escalated)


# ==========================================================================
# 演示 5：高风险操作没有安全降级路径 -> 明确失败
# ==========================================================================


def demo_5_never_fake_success() -> None:
    trace.section(
        "演示 5：高风险写操作 + 无安全降级路径 —— 明确失败，绝不伪造成功",
        "笔记 17.2.4 的底线：没有安全降级路径时明确失败，绝不能伪造成功结果",
    )
    def make_refund():
        """一个永远超时的退款工具。5-a / 5-b 各起一个新的（计数器分开看）。"""
        return make_scripted_tool(
            "refund_payment",
            "执行退款",
            plan=[timeout_fault()],
            payload={},
            parameters={
                "type": "object",
                "properties": {
                    "order_id": {"type": "string", "description": "订单号"},
                    "amount": {"type": "integer", "description": "退款金额（元）"},
                },
                "required": ["order_id", "amount"],
            },
            annotations={"read_only": False, "destructive": True},
        )

    # 那条降级路径返回的是 {"refunded": True} —— 一条看起来很棒的假数据。
    tempting_fake_success = Fallback("cache", "缓存", lambda: {"refunded": True})

    print()
    trace.section("5-a 没有转人工通道：只能明确失败")
    tool, counter = make_refund()
    executor = ToolExecutor(registry_with(tool), retry=RetryPolicy(max_attempts=2), clock=VirtualClock())
    outcome = executor.call(
        "refund_payment",
        {"order_id": "T001", "amount": 553},
        fallbacks=[tempting_fake_success],
        escalate=None,
        allow_degradation=False,  # 高风险写操作不允许降级
    )
    show_outcome(outcome, counter)
    trace.note("注意那个缓存降级路径：它返回 {refunded: True}。")
    trace.note("如果允许降级，Agent 就会拿一条假数据当退款成功报给用户 ——")
    trace.note("所以高风险写操作必须 allow_degradation=False，宁可明确失败。")

    # -- 5-b：不允许降级 ≠ 不允许转人工 --------------------------------
    print()
    trace.section(
        "5-b 同样的操作，但留着转人工通道",
        "「不允许降级」关掉的是降级链；「转人工」是另一回事，对写操作反而更必要",
    )
    escalated: list[str] = []

    def escalate(reason: str) -> str:
        escalated.append(reason)
        return f"HITL-{len(escalated):03d}"

    tool, counter = make_refund()
    executor = ToolExecutor(registry_with(tool), retry=RetryPolicy(max_attempts=2), clock=VirtualClock())
    outcome = executor.call(
        "refund_payment",
        {"order_id": "T001", "amount": 553},
        fallbacks=[tempting_fake_success],
        escalate=escalate,
        allow_degradation=False,
    )
    show_outcome(outcome, counter)
    trace.kv("人工工单", escalated)
    trace.note("降级链依然关着 —— 那条 {refunded: True} 的假成功没有被用上。")
    trace.note("但任务没有掉进「明确失败」，而是交到了人手上。")
    trace.note("")
    trace.note("这两件事以前是同一个开关，于是「不允许降级」顺带把转人工也关了。")
    trace.note("但它们的后果正好相反：降级是换一个更弱的能力顶上（数据可能不对），")
    trace.note("转人工是承认自动路径做不了、把决定权交回人。")
    trace.note("高风险写操作最不能接受的恰恰是「悄悄失败」—— 用户的钱卡在半路，")
    trace.note("必须有人接手，而不是回一句「失败了」就完事。")


# ==========================================================================
# 演示 6：模型服务故障 -> 重试 -> 切换备用模型 -> 权限降级
# ==========================================================================


def demo_6_model_failure() -> None:
    trace.section(
        "演示 6：模型服务故障 —— 受控重试 -> 切换备用模型 -> 权限降级",
        "笔记 17.3.1 / 17.3.3 / 17.3.4",
    )

    # 主模型每次调用都超时
    primary = ScriptedLLM([TimeoutError("read timeout")] * 20, name="primary-gpt", model="primary")
    # 备用模型能用，但复杂推理和规则遵循都弱一档
    backup = ScriptedLLM(
        [ToolCall("query_order", {"order_id": "T001"})], name="backup-mini", model="backup"
    )

    specs = {
        "primary-gpt": ModelSpec("primary-gpt", quality=0.95, verified_on="2026-09-01 全量回归"),
        "backup-mini": ModelSpec(
            "backup-mini",
            tool_calling=True,
            complex_reasoning=False,
            rule_following=False,
            quality=0.78,
            verified_on="2026-09-05 退款任务集（仅查询通过）",
        ),
    }
    clock = VirtualClock()
    gateway = ModelGateway(
        primary=primary,
        backups=[backup],
        specs=specs,
        retry_delays=[0.5, 1.0, 2.0],
        clock=clock,
    )
    result = gateway.call(
        [{"role": "user", "content": "帮我把订单 T001 退款"}],
        [],
        required_capability="tool_calling",
        needs_write=True,
    )

    print()
    for line in result.trace:
        print(f"    {line}")
    trace.rule("-")
    trace.kv("是否成功接管", result.ok)
    trace.kv("最终动作", result.action)
    trace.kv("接管模型", result.used_model)
    trace.kv("调整后的权限", result.permission.value)
    trace.kv("说明", result.detail)

    print()
    trace.note("现在用降级后的权限去执行一个高风险动作：")
    allowed, why = require_permission(result.permission, "执行退款", "write")
    trace.kv("执行退款", f"{'允许' if allowed else '拒绝'} —— {why}")
    allowed, why = require_permission(result.permission, "查询订单", "read")
    trace.kv("查询订单", f"{'允许' if allowed else '拒绝'} —— {why}")
    trace.note("笔记 17.3.4 的核心：启用备用模型不等于保持原有权限，")
    trace.note("备用能力下降时，Agent 的自动化权限也必须随之下降。")


# ==========================================================================
# 演示 7：接口正常但模型能力退化
# ==========================================================================


def demo_7_quality_degradation() -> None:
    trace.section(
        "演示 7：接口返回 200，但模型在业务上已经退化",
        "笔记 17.3.2：不能只监控可用率和响应时间",
    )

    monitor = QualityMonitor()
    # 模拟一批真实任务：接口都成功，但业务质量在下滑
    observations = [
        # (task_id, 任务成功, 参数正确, 违反规则, 输出)
        ("task-1", True, True, False, "允许退款"),
        ("task-1", True, True, False, "允许退款"),
        ("task-2", False, False, True, "允许退款"),  # 违反退款规则
        ("task-2", False, True, True, "拒绝退款"),  # 重复运行结果不一致
        ("task-3", True, False, False, "订单 T999 已退款"),  # 生成了不存在的订单号
        ("task-3", False, False, False, "订单 T999 已退款"),
        ("task-4", True, True, False, "允许退款"),
    ]
    for task_id, success, params_ok, violated, output in observations:
        monitor.observe(task_id, success=success, params_ok=params_ok, violated_rule=violated, output=output)

    print()
    trace.kv("监控指标", monitor.snapshot())
    degraded, why = monitor.verdict()
    print()
    trace.kv("结论", "已退化，按服务事故处理" if degraded else "正常")
    trace.kv("判定依据", why)
    trace.note("接口层面全部返回成功，但从业务指标看这个模型已经不能信任了 ——")
    trace.note("此时应当停止让它执行高风险写操作，而不是因为接口返回成功就继续放行。")

    print()
    trace.rule("-")
    print("  为什么不能只看单次成功率（笔记 17.3.2 引 t-bench 的结论）：")
    trace.summary_table(
        [
            {"单次成功率": f"{p:.0%}", "连续 1 次": f"{pass_k(p, 1):.1%}", "连续 3 次": f"{pass_k(p, 3):.1%}",
             "连续 5 次": f"{pass_k(p, 5):.1%}", "连续 10 次": f"{pass_k(p, 10):.1%}"}
            for p in (0.95, 0.85, 0.70)
        ],
        ["单次成功率", "连续 1 次", "连续 3 次", "连续 5 次", "连续 10 次"],
    )
    trace.note("同一个任务要连续多次都成功才算可靠 —— 单次 85% 的模型，")
    trace.note("连续 5 次全对的概率只剩 44%。这就是 Agent 可靠性不能只看平均值的原因。")


def main() -> None:
    trace.section(
        "原型 03：Agent 健壮性设计",
        "笔记第 17 章：工具调用失败 + 模型调用失败",
    )
    trace.note("整体处理思路四步：错误分类 -> 局部恢复 -> 故障隔离 -> 优雅降级")
    trace.note("一句话概括：先分类，能恢复就受控恢复；不能恢复就熔断；确实不可用就优雅降级。")

    demo_1_transient_retry()
    demo_1b_response_timeout()
    demo_2_deterministic()
    demo_3_circuit_breaker()
    demo_4_degradation_ladder()
    demo_5_never_fake_success()
    demo_6_model_failure()
    demo_7_quality_degradation()

    trace.section("八个演示对应的笔记位置")
    trace.summary_table(
        [
            {"演示": "1 瞬时故障重试", "笔记": "17.2.1", "关键产物": "指数退避 + 随机抖动 + 总预算"},
            {"演示": "1-b 单次响应超时", "笔记": "17.2.1", "关键产物": "timeout_s：迟到＝失败"},
            {"演示": "2 确定性错误", "笔记": "17.2.2", "关键产物": "结构化错误 + failed_fields"},
            {"演示": "3 熔断器", "笔记": "17.2.3", "关键产物": "closed/open/half_open 状态机"},
            {"演示": "4 逐级降级", "笔记": "17.2.4", "关键产物": "备用->缓存->部分结果->人工"},
            {"演示": "5 不伪造成功", "笔记": "17.2.4", "关键产物": "allow_degradation=False；不降级≠不转人工"},
            {"演示": "6 备用模型", "笔记": "17.3.3/17.3.4", "关键产物": "能力验证 + 权限降级"},
            {"演示": "7 质量退化", "笔记": "17.3.2", "关键产物": "业务指标 + pass^k"},
        ],
        ["演示", "笔记", "关键产物"],
    )


if __name__ == "__main__":
    main()
