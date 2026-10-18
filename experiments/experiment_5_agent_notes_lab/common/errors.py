"""Production-facing error taxonomy shared by the learning modules.

The examples intentionally keep the business domain small, but the boundary
between infrastructure failures and business failures is real.  Callers can
use ``retryable`` and ``kind`` instead of parsing exception strings.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ErrorKind(str, Enum):
    VALIDATION = "validation"
    AUTHORIZATION = "authorization"
    APPROVAL_REQUIRED = "approval_required"
    NOT_FOUND = "not_found"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    TRANSIENT = "transient"
    CONFLICT = "conflict"
    INTERNAL = "internal"


@dataclass
class AgentError(Exception):
    """An error safe to cross an agent/tool boundary."""

    code: str
    message: str
    kind: ErrorKind = ErrorKind.INTERNAL
    retryable: bool = False
    status: int | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        Exception.__init__(self, self.message)

    def safe_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "kind": self.kind.value,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.status is not None:
            payload["status"] = self.status
        if self.details:
            payload["details"] = self.details
        return payload


def as_agent_error(exc: BaseException) -> AgentError:
    """Normalize expected Python failures without leaking stack traces."""
    if isinstance(exc, AgentError):
        return exc
    if isinstance(exc, (TimeoutError,)):
        return AgentError("timeout", "上游调用超时，请稍后重试", ErrorKind.TIMEOUT, True)
    if isinstance(exc, (ConnectionError, OSError)):
        return AgentError("upstream_unavailable", "上游服务暂时不可用", ErrorKind.TRANSIENT, True)
    return AgentError("internal_error", "内部处理失败", ErrorKind.INTERNAL, False)
