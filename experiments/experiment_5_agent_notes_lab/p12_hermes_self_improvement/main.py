"""原型 12：HermesAgent 的记忆、Skill 和用户画像自进化闭环。"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from common import trace  # noqa: E402
from hermes_learning import HermesAgent, LearningStore, Skill  # noqa: E402


def main() -> None:
    trace.section("原型 12：Hermes 自进化循环", "第 12 章：Think → Act → Reflect → memory/skill/user model")
    store = LearningStore(
        skills={
            "research-summary": Skill("research-summary", "旧版本摘要流程", ["搜索", "直接输出"]),
        }
    )
    agent = HermesAgent(store, interval=3)
    trace.note("代码计数器决定何时反思；辅助模型只判断哪些内容值得沉淀。")
    trace.kv("主模型", agent.answer("研究并输出摘要"))

    steps = [
        ("search", "找到三个候选来源"),
        ("browser", "失败：页面超时"),
        ("search", "成功流程：去重后保留带证据的结论；用户偏好先给结论"),
        ("write", "决定：最终摘要按结论-证据-风险组织"),
    ]
    for index, (name, result) in enumerate(steps, start=1):
        due = agent.tool(name, result)
        trace.kv(f"工具 {index}: {name}", f"{result} | 是否触发反思={due}")
        if due:
            reflection = agent.reflect("研究并输出摘要")
            trace.kv("反思结果", reflection.reason)

    trace.section("三路沉淀")
    trace.kv("事实性 memory", store.facts)
    trace.kv("程序性 Skill", {name: skill.steps for name, skill in store.skills.items()})
    trace.kv("Honcho 用户画像（原型内存版）", store.user_preferences)
    trace.note("已有 research-summary 被更新，而不是重复创建；这正是治理 Skill 膨胀的关键。")

    trace.section("OpenClaw Flush 与 Hermes 反思的区别")
    trace.summary_table(
        [
            {"机制": "Pre-Compaction Flush", "触发": "上下文压缩前", "沉淀": "主要是 memory"},
            {"机制": "Hermes Reflection", "触发": "工具调用达到间隔", "沉淀": "memory + Skill + 用户画像"},
        ],
        ["机制", "触发", "沉淀"],
    )


if __name__ == "__main__":
    main()
