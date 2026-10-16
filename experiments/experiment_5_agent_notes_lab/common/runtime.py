"""Minimal production runtime context shared by P01-P19 examples."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from common.config import RuntimeConfig
from common.observability import JsonLogger, Metrics, TraceContext
from common.storage import JsonlStore


@dataclass
class RunBudget:
    """Hard per-request limits; all loops must consume this object."""

    max_steps: int
    max_tool_calls: int
    deadline: float
    steps: int = 0
    tool_calls: int = 0

    @classmethod
    def from_config(cls, config: RuntimeConfig, *, now: float = time.monotonic) -> "RunBudget":
        return cls(config.max_steps, config.max_tool_calls, now() + config.request_timeout_s)

    def consume_step(self) -> None:
        if self.steps >= self.max_steps or time.monotonic() >= self.deadline:
            raise TimeoutError("agent run budget exhausted")
        self.steps += 1

    def consume_tool(self) -> None:
        if self.tool_calls >= self.max_tool_calls or time.monotonic() >= self.deadline:
            raise TimeoutError("tool call budget exhausted")
        self.tool_calls += 1

    @property
    def remaining_s(self) -> float:
        return max(0.0, self.deadline - time.monotonic())


@dataclass
class RunContext:
    config: RuntimeConfig
    trace: TraceContext = field(default_factory=TraceContext)
    metrics: Metrics = field(default_factory=Metrics)
    logger: JsonLogger = field(default_factory=lambda: JsonLogger(enabled=False))
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    budget: RunBudget | None = None

    def __post_init__(self) -> None:
        self.config.validate()
        self.budget = self.budget or RunBudget.from_config(self.config)


class AuditSink:
    """A small append-only audit boundary for security and lifecycle events."""

    def __init__(self, path: str):
        self._store = JsonlStore(path)

    def record(self, event: str, *, context: RunContext | None = None, **fields: Any) -> None:
        payload: dict[str, Any] = {"event": event, "ts": time.time(), **fields}
        if context:
            payload.update({"run_id": context.run_id, "trace_id": context.trace.trace_id})
        self._store.append(payload)
