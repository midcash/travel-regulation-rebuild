"""Typed failures shared by tool provider ports and their adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from .values import StableId, TraceId


@dataclass(frozen=True, slots=True)
class ToolErrorContext:
    """Safe identifiers attached to a failed provider operation."""

    provider: str
    operation: str
    trace_id: TraceId | None = None
    query_id: StableId | None = None


class ToolError(RuntimeError):
    """Base error for an external tool failure.

    Adapters must raise a specific subclass and preserve the original exception
    with ``raise ... from exc``. The message is intentionally limited to a safe
    summary; raw provider payloads and credentials do not belong here.
    """

    code: ClassVar[str] = "TOOL_ERROR"
    retryable: ClassVar[bool] = False

    def __init__(
        self,
        *,
        provider: str,
        operation: str,
        safe_message: str,
        trace_id: TraceId | None = None,
        query_id: StableId | None = None,
    ) -> None:
        self.context = ToolErrorContext(
            provider=provider,
            operation=operation,
            trace_id=trace_id,
            query_id=query_id,
        )
        self.safe_message = safe_message
        super().__init__(safe_message)

    @property
    def provider(self) -> str:
        """Return the provider identifier without exposing configuration."""
        return self.context.provider

    @property
    def operation(self) -> str:
        """Return the normalized provider operation name."""
        return self.context.operation

    @property
    def trace_id(self) -> TraceId | None:
        """Return the request trace identifier, if one was available."""
        return self.context.trace_id

    @property
    def query_id(self) -> StableId | None:
        """Return the provider query identifier, if one was available."""
        return self.context.query_id


class ToolConfigurationError(ToolError):
    """Raised when a provider cannot be assembled with valid configuration."""

    code = "TOOL_CONFIGURATION_ERROR"


class ToolTimeoutError(ToolError):
    """Raised when a provider operation exceeds its explicit timeout budget."""

    code = "TOOL_TIMEOUT_ERROR"
    retryable = True


class ToolTransportError(ToolError):
    """Raised when a provider request cannot reach or read the service."""

    code = "TOOL_TRANSPORT_ERROR"
    retryable = True


class ToolAuthenticationError(ToolError):
    """Raised when a provider rejects credentials or authorization."""

    code = "TOOL_AUTHENTICATION_ERROR"


class ToolRateLimitError(ToolError):
    """Raised when a provider rejects a request because of rate limits."""

    code = "TOOL_RATE_LIMIT_ERROR"
    retryable = True


class ToolResponseSchemaError(ToolError):
    """Raised when a provider response is not a valid normalized contract."""

    code = "TOOL_RESPONSE_SCHEMA_ERROR"


class ToolEmptyResultError(ToolError):
    """Raised when a successful provider response contains no usable results."""

    code = "TOOL_EMPTY_RESULT_ERROR"


class ToolConstraintNoMatchError(ToolError):
    """Raised when provider results exist but none satisfy a hard query constraint."""

    code = "TOOL_CONSTRAINT_NO_MATCH"


class ToolBusinessError(ToolError):
    """Raised when a provider returns a domain-level failure response."""

    code = "TOOL_BUSINESS_ERROR"


__all__ = [
    "ToolAuthenticationError",
    "ToolBusinessError",
    "ToolConfigurationError",
    "ToolConstraintNoMatchError",
    "ToolEmptyResultError",
    "ToolError",
    "ToolErrorContext",
    "ToolRateLimitError",
    "ToolResponseSchemaError",
    "ToolTimeoutError",
    "ToolTransportError",
]
