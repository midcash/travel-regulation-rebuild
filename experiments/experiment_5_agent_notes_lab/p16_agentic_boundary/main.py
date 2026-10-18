"""原型 16：回答“这是 Agent 还是 Workflow，为什么不用传统方法”。"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from common import trace  # noqa: E402
from decision_modes import AdaptiveAgent, FixedWorkflow, UseCase, choose_architecture, compound_success  # noqa: E402


def main() -> None:
    trace.section("原型 16：Agentic 边界与架构选型", "第 16 章：Agentic 程度、Workflow/Agent、传统方法边界")
    cases = [
        UseCase("时间段是否重叠", False, False, False, False, False),
        UseCase("旅行自然语言规划", True, True, True, False, True),
        UseCase("支付欺诈处置", True, True, True, True, True),
    ]
    for case in cases:
        choice = choose_architecture(case)
        trace.summary_table(
            [{"场景": case.name, "选择": choice.mode, "依据": "；".join(choice.reasons)}],
            ["场景", "选择", "依据"],
        )
        trace.note("护栏：" + "、".join(choice.guardrails))

    request = "我想订一张去苏黎世的机票"
    trace.section("同一请求：固定 Workflow 与自适应 Agent")
    trace.kv("固定 Workflow", FixedWorkflow().run(request))
    trace.kv("自适应 Agent", AdaptiveAgent().run(request))
    trace.note("Agent 不是“用了 LLM”就成立，关键是模型能否根据当前状态改变下一步。")

    trace.section("复合错误率")
    trace.summary_table(
        [{"单步正确率": "95%", "步骤数": n, "整体成功率": f"{compound_success(0.95, n):.2%}"} for n in (1, 10, 100)],
        ["单步正确率", "步骤数", "整体成功率"],
    )
    trace.note("窄域、高风险任务主动收窄自主性，是可靠性设计，不是“做不成真 Agent”。")


if __name__ == "__main__":
    main()
