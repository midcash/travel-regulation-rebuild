"""Agentic 程度与 Workflow/Agent 选型原型。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class UseCase:
    name: str
    nuanced_judgment: bool
    unstructured_input: bool
    rules_hard_to_maintain: bool
    high_stakes: bool
    path_open_ended: bool


@dataclass(frozen=True)
class ArchitectureChoice:
    mode: str
    reasons: tuple[str, ...]
    guardrails: tuple[str, ...]


def choose_architecture(case: UseCase) -> ArchitectureChoice:
    reasons: list[str] = []
    if case.nuanced_judgment:
        reasons.append("需要上下文判断，不只是查表")
    if case.unstructured_input:
        reasons.append("输入包含自然语言或非结构化文档")
    if case.rules_hard_to_maintain:
        reasons.append("规则变化快且维护成本持续增长")
    if case.high_stakes:
        mode = "混合 Workflow + 有界 Agent"
        reasons.append("高风险场景必须保留确定性路由、审批和审计")
    elif not reasons:
        mode = "确定性 Workflow / 传统规则"
        reasons.append("流程可提前写死，LLM 没有不可替代的判断价值")
    elif case.path_open_ended:
        mode = "有护栏的 Agent"
        reasons.append("路径开放，模型需要根据中间结果决定下一步")
    else:
        mode = "混合 Workflow + LLM"
        reasons.append("让 LLM 负责语义判断，代码负责执行骨架")
    return ArchitectureChoice(
        mode,
        tuple(reasons),
        ("最大轮数/预算", "工具 allowlist", "高风险动作人工确认", "输出结构校验"),
    )


def compound_success(step_success: float, steps: int) -> float:
    return step_success**steps


class FixedWorkflow:
    def run(self, request: str) -> list[str]:
        return ["extract_fields", "call_search", "render_answer"]


class AdaptiveAgent:
    def run(self, request: str) -> list[str]:
        actions = ["inspect_request"]
        if "苏黎世" in request and "出发" not in request:
            actions.append("ask_clarification")
        else:
            actions.extend(["search", "compare", "answer"])
        return actions
