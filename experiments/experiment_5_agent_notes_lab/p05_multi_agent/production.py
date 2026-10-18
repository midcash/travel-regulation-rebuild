"""Bounded supervisor for parallel specialist agents."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable

from common.config import RuntimeConfig
from common.errors import as_agent_error
from common.observability import TraceContext
from common.resilience import RetryPolicy, retry_call, call_with_timeout


@dataclass(frozen=True)
class WorkerSpec:
    name: str
    run: Callable[[str], Any]
    critical: bool = True


@dataclass
class WorkerResult:
    name: str
    ok: bool
    value: Any = None
    error: dict[str, Any] = field(default_factory=dict)
    attempts: int = 0


@dataclass
class SupervisorResult:
    status: str
    results: list[WorkerResult]
    trace_id: str


class ProductionSupervisor:
    def __init__(self, workers: list[WorkerSpec], *, config: RuntimeConfig | None = None):
        names = [worker.name for worker in workers]
        if len(names) != len(set(names)):
            raise ValueError("worker names must be unique")
        self.workers = workers
        self.config = (config or RuntimeConfig()).validate()

    def run(self, task: str, *, trace: TraceContext | None = None) -> SupervisorResult:
        trace = trace or TraceContext()
        def execute(worker: WorkerSpec) -> WorkerResult:
            attempts = 0
            try:
                def operation() -> Any:
                    nonlocal attempts
                    attempts += 1
                    return call_with_timeout(lambda: worker.run(task), self.config.request_timeout_s)
                value = retry_call(operation, policy=RetryPolicy(max_attempts=self.config.retry_attempts, total_timeout_s=self.config.retry_total_timeout_s, jitter_ratio=0.0))
                return WorkerResult(worker.name, True, value, attempts=attempts)
            except BaseException as exc:
                return WorkerResult(worker.name, False, error=as_agent_error(exc).safe_dict(), attempts=attempts)

        results: list[WorkerResult] = []
        with ThreadPoolExecutor(max_workers=min(8, max(1, len(self.workers))), thread_name_prefix="agent-worker") as pool:
            futures = {pool.submit(execute, worker): worker for worker in self.workers}
            for future in as_completed(futures):
                results.append(future.result())
        results.sort(key=lambda item: item.name)
        status = "failed" if any(not item.ok and next(worker for worker in self.workers if worker.name == item.name).critical for item in results) else "completed"
        return SupervisorResult(status, results, trace.trace_id)
