"""Retry, timeout, bulkhead and circuit-breaker building blocks.

The breaker is deliberately call-driven: there is no background timer thread.
When a request arrives, ``before_call`` checks whether the recovery window has
elapsed and admits one probe.  This keeps idle services cheap while retaining
time-based recovery semantics.  A distributed deployment should move the
state to a shared store or use a service-mesh breaker.
"""
from __future__ import annotations

import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Iterator, TypeVar

from common.errors import AgentError, ErrorKind, as_agent_error

T = TypeVar("T")


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(AgentError):
    def __init__(self, name: str):
        super().__init__("circuit_open", f"{name} 当前熔断，请稍后重试", ErrorKind.TRANSIENT, True)


class BulkheadFullError(AgentError):
    def __init__(self, name: str):
        super().__init__("bulkhead_full", f"{name} 并发已满，请稍后重试", ErrorKind.RATE_LIMITED, True)


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    total_timeout_s: float = 10.0
    backoff_base_s: float = 0.05
    backoff_max_s: float = 1.0
    jitter_ratio: float = 0.2

    def validate(self) -> "RetryPolicy":
        if self.max_attempts < 1 or self.total_timeout_s <= 0 or self.backoff_base_s < 0 or self.backoff_max_s < 0:
            raise ValueError("invalid retry policy")
        return self


class CircuitBreaker:
    """Thread-safe circuit breaker with one half-open probe at a time."""

    def __init__(self, name: str, *, failure_threshold: int = 3, recovery_s: float = 15.0, clock: Callable[[], float] = time.monotonic):
        if failure_threshold < 1 or recovery_s <= 0:
            raise ValueError("invalid circuit breaker settings")
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_s = recovery_s
        self._clock = clock
        self._lock = threading.Lock()
        self.state = CircuitState.CLOSED
        self.failures = 0
        self.opened_at = 0.0
        self._probe_in_flight = False

    def before_call(self) -> None:
        with self._lock:
            if self.state is CircuitState.OPEN:
                if self._clock() - self.opened_at < self.recovery_s:
                    raise CircuitOpenError(self.name)
                if self._probe_in_flight:
                    raise CircuitOpenError(self.name)
                self.state = CircuitState.HALF_OPEN
                self._probe_in_flight = True
            elif self.state is CircuitState.HALF_OPEN:
                if self._probe_in_flight:
                    raise CircuitOpenError(self.name)
                self._probe_in_flight = True

    def record_success(self) -> None:
        with self._lock:
            self.state = CircuitState.CLOSED
            self.failures = 0
            self._probe_in_flight = False

    def record_failure(self, exc: BaseException) -> None:
        with self._lock:
            self._probe_in_flight = False
            if self.state is CircuitState.HALF_OPEN or self.failures + 1 >= self.failure_threshold:
                self.state = CircuitState.OPEN
                self.opened_at = self._clock()
                self.failures = self.failure_threshold
            else:
                self.failures += 1

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {"name": self.name, "state": self.state.value, "failures": self.failures, "opened_at": self.opened_at}


class Bulkhead:
    def __init__(self, name: str, max_concurrency: int):
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")
        self.name = name
        self._slots = threading.BoundedSemaphore(max_concurrency)

    @contextmanager
    def slot(self) -> Iterator[None]:
        if not self._slots.acquire(blocking=False):
            raise BulkheadFullError(self.name)
        try:
            yield
        finally:
            self._slots.release()


def retry_call(
    operation: Callable[[], T],
    *,
    policy: RetryPolicy,
    retry_if: Callable[[BaseException], bool] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> T:
    """Retry only classified transient failures and respect a total budget."""
    policy.validate()
    retry_if = retry_if or (lambda exc: as_agent_error(exc).retryable)
    started = clock()
    last: BaseException | None = None
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return operation()
        except BaseException as exc:
            last = exc
            if attempt >= policy.max_attempts or not retry_if(exc):
                raise
            remaining = policy.total_timeout_s - (clock() - started)
            if remaining <= 0:
                break
            delay = min(policy.backoff_max_s, policy.backoff_base_s * (2 ** (attempt - 1)))
            if policy.jitter_ratio:
                delay *= 1 + random.uniform(-policy.jitter_ratio, policy.jitter_ratio)
            sleep(min(max(0.0, delay), remaining))
    assert last is not None
    raise last


def resilient_call(
    operation: Callable[[], T],
    *,
    breaker: CircuitBreaker,
    bulkhead: Bulkhead | None = None,
    retry_policy: RetryPolicy = RetryPolicy(),
) -> T:
    def once() -> T:
        breaker.before_call()
        try:
            result = operation()
        except BaseException as exc:
            breaker.record_failure(exc)
            raise
        breaker.record_success()
        return result

    if bulkhead is None:
        return retry_call(once, policy=retry_policy)
    with bulkhead.slot():
        return retry_call(once, policy=retry_policy)


def call_with_timeout(operation: Callable[[], T], timeout_s: float, *, on_late_result: Callable[[T | None, BaseException | None], None] | None = None) -> T:
    """Bound a synchronous dependency call at the adapter boundary.

    Cancellation cannot stop arbitrary Python code already running in a
    worker thread, so real adapters should also pass a timeout to their HTTP
    client.  This guard still protects the caller and makes that limitation
    explicit instead of pretending a future cancellation is transactional.
    """
    if timeout_s <= 0:
        raise ValueError("timeout_s must be positive")
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="agent-timeout")
    try:
        future = pool.submit(operation)
        try:
            return future.result(timeout=timeout_s)
        except FutureTimeout as exc:
            future.cancel()
            if on_late_result is not None:
                def reconcile(done) -> None:
                    try:
                        on_late_result(done.result(), None)
                    except BaseException as late_exc:
                        on_late_result(None, late_exc)

                future.add_done_callback(reconcile)
            raise TimeoutError(f"operation exceeded {timeout_s:.3f}s") from exc
    finally:
        # Do not wait for arbitrary user code after the caller's deadline.
        # The underlying adapter still needs its own HTTP/SDK timeout.
        pool.shutdown(wait=False, cancel_futures=True)
