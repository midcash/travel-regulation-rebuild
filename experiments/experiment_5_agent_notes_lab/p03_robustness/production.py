"""Provider gateway used to demonstrate real production failure handling."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from common.config import RuntimeConfig
from common.resilience import Bulkhead, CircuitBreaker, RetryPolicy, resilient_call, call_with_timeout


@dataclass
class ProviderHealth:
    name: str
    calls: int = 0
    failures: int = 0
    breaker_state: str = "closed"


@dataclass
class ProviderSlot:
    name: str
    client: Any
    breaker: CircuitBreaker
    bulkhead: Bulkhead


class ProductionModelGateway:
    """Fail over across providers while preserving one request contract.

    Quality policy belongs above this layer; this layer only answers
    availability questions and emits health snapshots for operations.
    """

    def __init__(self, providers: list[tuple[str, Any]], *, config: RuntimeConfig | None = None):
        if not providers:
            raise ValueError("at least one provider is required")
        self.config = (config or RuntimeConfig()).validate()
        self.slots = [ProviderSlot(name, client, CircuitBreaker(name, failure_threshold=self.config.circuit_failure_threshold, recovery_s=self.config.circuit_recovery_s), Bulkhead(name, 16)) for name, client in providers]
        self.health = {slot.name: ProviderHealth(slot.name) for slot in self.slots}

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> Any:
        last: BaseException | None = None
        for slot in self.slots:
            health = self.health[slot.name]
            health.calls += 1
            try:
                result = resilient_call(
                    lambda slot=slot: call_with_timeout(lambda: slot.client.chat(messages, tools or []), self.config.request_timeout_s),
                    breaker=slot.breaker,
                    bulkhead=slot.bulkhead,
                    retry_policy=RetryPolicy(max_attempts=self.config.retry_attempts, total_timeout_s=self.config.retry_total_timeout_s, jitter_ratio=0.0),
                )
                health.breaker_state = slot.breaker.snapshot()["state"]
                return result
            except BaseException as exc:
                health.failures += 1
                health.breaker_state = slot.breaker.snapshot()["state"]
                last = exc
        assert last is not None
        raise last

    def health_snapshot(self) -> list[dict[str, Any]]:
        return [vars(item).copy() for item in self.health.values()]
