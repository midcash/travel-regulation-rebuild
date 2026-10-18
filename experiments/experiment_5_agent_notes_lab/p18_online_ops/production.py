"""Production operations view: SLOs, tail latency and safe attribution."""
from __future__ import annotations

from dataclasses import dataclass
from statistics import quantiles
from typing import Any

from common.observability import redact


@dataclass(frozen=True)
class RequestSample:
    route: str
    latency_ms: float
    ok: bool
    model: str
    tool: str = ""


@dataclass
class SLOReport:
    total: int
    error_rate: float
    p95_latency_ms: float
    passed: bool
    labels: dict[str, Any]


class ProductionOpsReport:
    def __init__(self, *, max_error_rate: float = 0.02, max_p95_ms: float = 3000):
        self.max_error_rate = max_error_rate
        self.max_p95_ms = max_p95_ms

    def build(self, samples: list[RequestSample]) -> SLOReport:
        if not samples:
            return SLOReport(0, 1.0, 0.0, False, {"reason": "no_traffic"})
        latencies = sorted(sample.latency_ms for sample in samples)
        p95 = latencies[-1] if len(latencies) < 2 else quantiles(latencies, n=100, method="inclusive")[94]
        errors = sum(not sample.ok for sample in samples) / len(samples)
        labels = redact({"routes": sorted({sample.route for sample in samples}), "models": sorted({sample.model for sample in samples})})
        return SLOReport(len(samples), round(errors, 4), round(p95, 2), errors <= self.max_error_rate and p95 <= self.max_p95_ms, labels)
