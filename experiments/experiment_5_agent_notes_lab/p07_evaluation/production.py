"""Evaluation harness with versioned cases and fail-closed scoring."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable

from common.errors import AgentError, ErrorKind


@dataclass(frozen=True)
class EvalCase:
    case_id: str
    input: str
    expected: str
    rubric_version: str = "v1"


@dataclass
class CaseResult:
    case_id: str
    passed: bool
    score: float
    output: str = ""
    error: dict[str, Any] = field(default_factory=dict)


@dataclass
class EvalReport:
    dataset_hash: str
    passed: int
    total: int
    pass_rate: float
    gate_passed: bool
    cases: list[CaseResult]


class ProductionEvaluator:
    def __init__(self, runner: Callable[[str], str], scorer: Callable[[EvalCase, str], float], *, min_pass_rate: float = 0.8):
        if not 0 <= min_pass_rate <= 1:
            raise ValueError("min_pass_rate must be between 0 and 1")
        self.runner = runner
        self.scorer = scorer
        self.min_pass_rate = min_pass_rate

    def run(self, cases: list[EvalCase]) -> EvalReport:
        if not cases or len({case.case_id for case in cases}) != len(cases):
            raise AgentError("invalid_dataset", "评测集不能为空且 case_id 必须唯一", ErrorKind.VALIDATION)
        dataset_hash = hashlib.sha256(json.dumps([vars(case) for case in cases], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        results: list[CaseResult] = []
        for case in cases:
            try:
                output = self.runner(case.input)
                score = float(self.scorer(case, output))
                if not 0 <= score <= 1:
                    raise ValueError("score outside [0, 1]")
                results.append(CaseResult(case.case_id, score >= 0.5, score, output))
            except BaseException as exc:
                results.append(CaseResult(case.case_id, False, 0.0, error={"type": type(exc).__name__, "message": str(exc)}))
        passed = sum(item.passed for item in results)
        rate = passed / len(results)
        return EvalReport(dataset_hash, passed, len(results), rate, rate >= self.min_pass_rate, results)
