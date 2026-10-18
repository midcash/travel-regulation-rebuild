"""Production decision boundary: deterministic workflows first, agents by exception."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Workload:
    name: str
    deterministic_steps: int
    ambiguity: float
    external_side_effects: bool
    audit_required: bool


@dataclass(frozen=True)
class BoundaryDecision:
    mode: str
    reasons: tuple[str, ...]
    safeguards: tuple[str, ...]


class ProductionBoundary:
    def choose(self, workload: Workload) -> BoundaryDecision:
        reasons: list[str] = []
        safeguards: list[str] = ["timeout", "idempotency", "structured logs"]
        if workload.ambiguity > 0.45:
            reasons.append("存在高歧义，需要模型做语义判断")
        if workload.deterministic_steps > 8:
            reasons.append("流程分支较多，固定流程维护成本上升")
        if workload.external_side_effects:
            safeguards += ["allowlist", "approval gate"]
        if workload.audit_required:
            safeguards.append("append-only audit")
        mode = "agent" if reasons else "workflow"
        return BoundaryDecision(mode, tuple(reasons or ["步骤稳定且规则明确"]), tuple(dict.fromkeys(safeguards)))
