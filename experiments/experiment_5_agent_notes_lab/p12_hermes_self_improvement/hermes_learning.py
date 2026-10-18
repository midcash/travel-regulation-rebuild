"""Hermes 的自进化循环：周期性反思，分别沉淀事实、Skill 和用户画像。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Skill:
    name: str
    description: str
    steps: list[str]
    uses: int = 0


@dataclass
class LearningStore:
    facts: list[str] = field(default_factory=list)
    skills: dict[str, Skill] = field(default_factory=dict)
    user_preferences: dict[str, str] = field(default_factory=dict)

    def save_fact(self, fact: str) -> None:
        if fact and fact not in self.facts:
            self.facts.append(fact)

    def upsert_skill(self, skill: Skill) -> str:
        old = self.skills.get(skill.name)
        if old is None:
            self.skills[skill.name] = skill
            return "created"
        old.steps = skill.steps
        old.description = skill.description
        return "updated"


@dataclass
class Reflection:
    worth_saving: bool
    fact: str = ""
    skill: Skill | None = None
    preference: tuple[str, str] | None = None
    reason: str = ""


class AuxiliaryModel:
    """用轻量规则模拟辅助模型：主模型做任务，辅助模型做摘要/分类/反思。"""

    def reflect(self, task: str, observations: list[str]) -> Reflection:
        joined = " ".join(observations)
        if not any(word in joined for word in ("成功", "失败", "偏好", "决定")):
            return Reflection(False, reason="Nothing to save")
        fact = ""
        if "失败" in joined:
            fact = "这次任务曾因外部工具失败，下一次先检查工具返回值再继续。"
        elif "决定" in joined:
            fact = "本次任务产生了一个可供后续检索的业务决定。"
        skill = None
        if "成功流程" in joined:
            skill = Skill(
                name="research-summary",
                description="把检索结果整理成带证据的摘要",
                steps=["检索资料", "去重证据", "按结论-证据-风险输出"],
            )
        preference = ("style", "用户偏好先给结论，再给简短依据") if "偏好" in joined else None
        return Reflection(True, fact=fact, skill=skill, preference=preference, reason="有可复用的事实或流程")


class PrimaryModel:
    """主模型只负责面向用户的长链任务；这里用确定性文本替身。"""

    def answer(self, task: str) -> str:
        return f"主模型完成任务：{task}"


class ReflectionScheduler:
    def __init__(self, interval: int = 3) -> None:
        self.interval = interval
        self.tool_calls = 0

    def record_tool_call(self) -> bool:
        self.tool_calls += 1
        if self.tool_calls >= self.interval:
            self.tool_calls = 0
            return True
        return False


class HermesAgent:
    """极简 Hermes：反思由代码计数器触发，语义判断交给辅助模型。"""

    def __init__(self, store: LearningStore, interval: int = 3) -> None:
        self.store = store
        self.primary = PrimaryModel()
        self.scheduler = ReflectionScheduler(interval)
        self.auxiliary = AuxiliaryModel()
        self.events: list[str] = []
        self.reflections: list[Reflection] = []

    def answer(self, task: str) -> str:
        return self.primary.answer(task)

    def tool(self, name: str, result: str) -> bool:
        self.events.append(f"{name}: {result}")
        return self.scheduler.record_tool_call()

    def reflect(self, task: str) -> Reflection:
        decision = self.auxiliary.reflect(task, self.events)
        self.reflections.append(decision)
        if not decision.worth_saving:
            return decision
        self.store.save_fact(decision.fact)
        if decision.skill:
            self.store.upsert_skill(decision.skill)
        if decision.preference:
            key, value = decision.preference
            self.store.user_preferences[key] = value
        return decision

    def run(self, task: str, steps: list[tuple[str, str]]) -> list[Reflection]:
        """主回复可以先结束；这里用同步调用模拟后台反思，便于调试。"""
        for name, result in steps:
            due = self.tool(name, result)
            if due:
                self.reflect(task)
        if self.scheduler.tool_calls:
            self.reflect(task)
        return self.reflections
