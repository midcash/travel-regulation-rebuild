"""Evidence-based framework selection, kept independent from vendor SDKs."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FrameworkOption:
    name: str
    graph_control: int
    ecosystem: int
    ops_maturity: int
    learning_cost: int


@dataclass(frozen=True)
class FrameworkDecision:
    selected: str
    scores: dict[str, float]
    assumptions: tuple[str, ...]


class ProductionFrameworkAdvisor:
    def choose(self, options: list[FrameworkOption], *, need_graph: bool, need_ops: bool) -> FrameworkDecision:
        if not options:
            raise ValueError("framework options cannot be empty")
        scores: dict[str, float] = {}
        for option in options:
            score = option.ecosystem * 0.25 + option.learning_cost * 0.15
            score += option.graph_control * (0.35 if need_graph else 0.10)
            score += option.ops_maturity * (0.40 if need_ops else 0.20)
            scores[option.name] = round(score, 3)
        selected = max(scores, key=scores.get)
        return FrameworkDecision(selected, scores, ("权重需要结合团队经验和现有基础设施复核", "最终上线前补做负载、安全和故障演练"))
