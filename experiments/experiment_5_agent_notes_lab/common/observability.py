"""Dependency-free structured observability primitives for the examples."""
from __future__ import annotations

import json
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator, TextIO


SENSITIVE_KEYS = {"authorization", "api_key", "token", "password", "secret"}


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: "[REDACTED]" if key.lower() in SENSITIVE_KEYS else redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


@dataclass(frozen=True)
class TraceContext:
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    parent_span_id: str = ""

    def child(self) -> "TraceContext":
        return TraceContext(self.trace_id, self.request_id, uuid.uuid4().hex[:16])


@dataclass
class Metrics:
    counters: dict[str, int] = field(default_factory=dict)
    timings_ms: dict[str, list[float]] = field(default_factory=dict)

    def inc(self, name: str, value: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + value

    def observe_ms(self, name: str, value: float) -> None:
        self.timings_ms.setdefault(name, []).append(round(value, 3))

    def snapshot(self) -> dict[str, Any]:
        return {"counters": dict(self.counters), "timings_ms": {k: list(v) for k, v in self.timings_ms.items()}}


class JsonLogger:
    def __init__(self, stream: TextIO | None = None, *, enabled: bool = True):
        self.stream = stream
        self.enabled = enabled

    def log(self, level: str, event: str, *, trace: TraceContext | None = None, **fields: Any) -> None:
        if not self.enabled:
            return
        payload: dict[str, Any] = {"ts": time.time(), "level": level, "event": event}
        if trace:
            payload.update({"trace_id": trace.trace_id, "request_id": trace.request_id})
        payload.update(redact(fields))
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=self.stream or sys.stdout)

    def info(self, event: str, **fields: Any) -> None:
        self.log("INFO", event, **fields)

    def warning(self, event: str, **fields: Any) -> None:
        self.log("WARN", event, **fields)


@contextmanager
def span(metrics: Metrics, name: str, *, trace: TraceContext | None = None, logger: JsonLogger | None = None, **fields: Any) -> Iterator[None]:
    started = time.perf_counter()
    if logger:
        logger.log("INFO", "span.started", trace=trace, span=name, **fields)
    try:
        yield
    except Exception as exc:
        metrics.inc(f"{name}.error")
        if logger:
            logger.log("ERROR", "span.failed", trace=trace, span=name, error_type=type(exc).__name__)
        raise
    finally:
        elapsed = (time.perf_counter() - started) * 1000
        metrics.observe_ms(name, elapsed)
        if logger:
            logger.log("INFO", "span.finished", trace=trace, span=name, elapsed_ms=round(elapsed, 3))
