"""原型 15：信用卡欺诈检测 Agent 场景题。"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from common import trace  # noqa: E402
from fraud_pipeline import FraudOrchestrator, FeedbackStore, Transaction  # noqa: E402


def main() -> None:
    trace.section("原型 15：信用卡欺诈检测 Agent", "第 15 章：Gateway / Router / Worker / Critic / 决策 / 异步反馈")
    feedback = FeedbackStore()
    orchestrator = FraudOrchestrator(feedback)
    cases = [
        Transaction("t-001", "u-vip", 200, "CN", "CN", 0.05, False, 1, whitelisted=True),
        Transaction("t-002", "u-bad", 800, "CN", "CN", 0.90, False, 1, blacklisted=True),
        Transaction("t-003", "u-3", 9000, "US", "CN", 0.75, True, 6, network_flagged=True),
        Transaction("t-004", "u-4", 700, "CN", "CN", 0.45, True, 2),
    ]
    results = []
    for tx in cases:
        decision = orchestrator.classify(tx)
        results.append(decision)
        trace.summary_table(
            [{
                "交易": tx.id,
                "路由": decision.route,
                "融合分": f"{decision.risk_score:.2f}",
                "Critic": f"{decision.critic_score:.2f}",
                "动作": decision.action,
            }],
            ["交易", "路由", "融合分", "Critic", "动作"],
        )
        trace.note(decision.rationale)
        if decision.evidence:
            trace.note("Worker 摘要：" + "；".join(f"{e.dimension}={e.risk:.2f}" for e in decision.evidence))

    trace.section("事后真实标签回流")
    tx = cases[-1]
    feedback_result = feedback.apply_label(tx, "fraud", results[-1])
    trace.kv("回流结果", feedback_result)
    trace.kv("账户画像计数", feedback.profile)
    trace.kv("欺诈模式库", feedback.patterns)
    trace.kv("当前拒绝阈值", feedback.reject_threshold)
    trace.note("实时链路只返回动作；真实欺诈标签几小时或几天后回流，用于更新画像、模式和阈值。")


if __name__ == "__main__":
    main()
