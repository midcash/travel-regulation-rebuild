"""Agent 上线后的指标、反馈、归因与修复闭环。"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from statistics import mean


@dataclass(frozen=True)
class Trace:
    id: str
    success: bool
    latency_ms: int
    cost_usd: float
    tokens: int
    failed_stage: str = ""
    safety_violations: int = 0
    fallback: bool = False
    user_rating: int | None = None


@dataclass(frozen=True)
class Metrics:
    pass_rate: float
    p95_latency: int
    avg_cost: float
    avg_tokens: float
    safety_violations: int
    fallback_rate: float
    positive_feedback_rate: float


@dataclass(frozen=True)
class Alert:
    metric: str
    detail: str


class Monitor:
    def __init__(self, baseline: Metrics) -> None:
        self.baseline = baseline

    @staticmethod
    def measure(traces: list[Trace]) -> Metrics:
        ordered = sorted(t.latency_ms for t in traces)
        p95 = ordered[int(0.95 * (len(ordered) - 1))] if ordered else 0
        rated = [t for t in traces if t.user_rating is not None]
        return Metrics(
            sum(t.success for t in traces) / len(traces),
            p95,
            mean(t.cost_usd for t in traces),
            mean(t.tokens for t in traces),
            sum(t.safety_violations for t in traces),
            sum(t.fallback for t in traces) / len(traces),
            sum((t.user_rating or 0) >= 4 for t in rated) / len(rated) if rated else 0.0,
        )

    def check(self, metrics: Metrics) -> list[Alert]:
        alerts: list[Alert] = []
        if metrics.pass_rate < self.baseline.pass_rate - 0.05:
            alerts.append(Alert("业务通过率", f"{self.baseline.pass_rate:.1%} -> {metrics.pass_rate:.1%}"))
        if metrics.p95_latency > self.baseline.p95_latency * 1.3:
            alerts.append(Alert("P95 延迟", f"{self.baseline.p95_latency}ms -> {metrics.p95_latency}ms"))
        if metrics.safety_violations:
            alerts.append(Alert("安全违规", f"出现 {metrics.safety_violations} 次，一票否决"))
        if metrics.avg_cost > self.baseline.avg_cost * 1.25:
            alerts.append(Alert("成本", f"${self.baseline.avg_cost:.4f} -> ${metrics.avg_cost:.4f}"))
        if metrics.positive_feedback_rate < self.baseline.positive_feedback_rate - 0.05:
            alerts.append(Alert("用户正反馈", f"{self.baseline.positive_feedback_rate:.1%} -> {metrics.positive_feedback_rate:.1%}"))
        return alerts


class Attribution:
    def diagnose(self, traces: list[Trace], alerts: list[Alert]) -> tuple[str, list[str]]:
        failures = Counter(t.failed_stage for t in traces if t.failed_stage)
        if any(alert.metric == "安全违规" for alert in alerts):
            return "安全策略/提示词注入", ["立即收紧高风险工具权限", "保留原始轨迹并回滚可疑版本"]
        if failures.get("tool", 0) >= 2:
            return "外部工具或依赖", ["检查工具超时/429/熔断", "把失败样本加入回归集"]
        if failures.get("retrieval", 0) >= 2:
            return "RAG/数据漂移", ["检查索引新鲜度和召回", "增加低置信人工兜底"]
        return "模型或 Prompt 变化", ["对比模型版本与 Prompt diff", "灰度回放后再决定回滚"]


def make_window(prefix: str, success_count: int, latency: int, failed_stage: str = "", safety: int = 0) -> list[Trace]:
    traces: list[Trace] = []
    for i in range(10):
        ok = i < success_count
        traces.append(
            Trace(
                f"{prefix}-{i+1}", ok, latency + (i % 3) * 40, 0.010 if ok else 0.016,
                900 if ok else 1400, failed_stage if not ok else "", safety if i == 0 else 0,
                fallback=not ok, user_rating=5 if ok else 2,
            )
        )
    return traces
