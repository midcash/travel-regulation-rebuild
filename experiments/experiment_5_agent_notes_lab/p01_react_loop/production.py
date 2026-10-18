"""Production-shaped ReAct runner: bounded, observable and policy-aware."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from common.config import RuntimeConfig
from common.errors import AgentError, ErrorKind, as_agent_error
from common.observability import Metrics, TraceContext
from common.resilience import Bulkhead, CircuitBreaker, RetryPolicy, resilient_call, call_with_timeout
from common.runtime import RunBudget


@dataclass
class ProductionLoopResult:
    status: str
    answer: str = ""
    reason: str = ""
    steps: int = 0
    tool_calls: int = 0
    trace_id: str = ""
    errors: list[dict[str, Any]] = field(default_factory=list)


class ProductionReActRunner:
    """The model chooses; the runtime validates, executes and limits."""

    def __init__(self, llm: Any, registry: Any, *, config: RuntimeConfig | None = None, allowed_tools: set[str] | None = None):
        self.llm = llm
        self.registry = registry
        self.config = (config or RuntimeConfig()).validate()
        self.allowed_tools = allowed_tools
        self.breaker = CircuitBreaker("react-llm", failure_threshold=2, recovery_s=5.0)
        self.bulkhead = Bulkhead("react-llm", max_concurrency=8)
        self.metrics = Metrics()

    def run(self, question: str, *, trace: TraceContext | None = None) -> ProductionLoopResult:
        if not question.strip():
            return ProductionLoopResult("failed", reason="empty_input")
        trace = trace or TraceContext()
        messages: list[dict[str, Any]] = [{"role": "user", "content": question.strip()}]
        budget = RunBudget.from_config(self.config)
        seen_calls: set[str] = set()

        while True:
            try:
                budget.consume_step()
                schemas = self.registry.schemas()
                response = resilient_call(
                    lambda: call_with_timeout(lambda: self.llm.chat(messages, schemas), min(self.config.request_timeout_s, max(0.1, budget.remaining_s))),
                    breaker=self.breaker,
                    bulkhead=self.bulkhead,
                    retry_policy=RetryPolicy(max_attempts=self.config.retry_attempts, total_timeout_s=min(self.config.retry_total_timeout_s, max(0.1, budget.remaining_s)), jitter_ratio=0.0),
                )
                messages.append(response.as_message())
                if not response.tool_calls:
                    self.metrics.inc("react.completed")
                    return ProductionLoopResult("completed", response.content, "model_answer", budget.steps, budget.tool_calls, trace.trace_id)

                for call in response.tool_calls:
                    budget.consume_tool()
                    if self.allowed_tools is not None and call.name not in self.allowed_tools:
                        raise AgentError("tool_not_allowed", f"工具 {call.name!r} 不在 allowlist 中", ErrorKind.AUTHORIZATION)
                    fingerprint = hashlib.sha256((call.name + json.dumps(call.arguments, sort_keys=True, ensure_ascii=False)).encode()).hexdigest()
                    if fingerprint in seen_calls:
                        raise AgentError("no_progress", "检测到重复工具调用，已停止避免循环烧钱", ErrorKind.CONFLICT)
                    seen_calls.add(fingerprint)
                    result = self.registry.call(call.name, call.arguments)
                    messages.append(result.to_message(call.id, call.name))
                self.metrics.inc("react.tool_batch", len(response.tool_calls))
            except BaseException as exc:
                error = as_agent_error(exc)
                self.metrics.inc("react.failed")
                return ProductionLoopResult("failed", reason=error.code, steps=budget.steps, tool_calls=budget.tool_calls, trace_id=trace.trace_id, errors=[error.safe_dict()])
