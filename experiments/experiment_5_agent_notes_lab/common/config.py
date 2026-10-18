"""Small, typed runtime configuration boundary.

Environment variables are parsed once at process startup.  Keeping this
boundary explicit makes the examples easy to move behind an HTTP worker or a
queue consumer without sprinkling ``os.environ`` throughout business code.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from common.errors import AgentError, ErrorKind


def _positive_float(value: str, name: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise AgentError("invalid_config", f"{name} 必须是数字", ErrorKind.VALIDATION) from exc
    if parsed <= 0:
        raise AgentError("invalid_config", f"{name} 必须大于 0", ErrorKind.VALIDATION)
    return parsed


def _positive_int(value: str, name: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise AgentError("invalid_config", f"{name} 必须是整数", ErrorKind.VALIDATION) from exc
    if parsed <= 0:
        raise AgentError("invalid_config", f"{name} 必须大于 0", ErrorKind.VALIDATION)
    return parsed


@dataclass(frozen=True)
class RuntimeConfig:
    service_name: str = "agent-notes"
    model_name: str = "mock-1"
    request_timeout_s: float = 20.0
    max_steps: int = 8
    max_tool_calls: int = 16
    retry_attempts: int = 3
    retry_total_timeout_s: float = 10.0
    circuit_failure_threshold: int = 3
    circuit_recovery_s: float = 15.0
    require_approval_for_writes: bool = True

    def validate(self) -> "RuntimeConfig":
        if not self.service_name.strip() or not self.model_name.strip():
            raise AgentError("invalid_config", "服务名和模型名不能为空", ErrorKind.VALIDATION)
        for name, value in (
            ("request_timeout_s", self.request_timeout_s),
            ("retry_total_timeout_s", self.retry_total_timeout_s),
            ("circuit_recovery_s", self.circuit_recovery_s),
        ):
            if value <= 0:
                raise AgentError("invalid_config", f"{name} 必须大于 0", ErrorKind.VALIDATION)
        for name, value in (
            ("max_steps", self.max_steps),
            ("max_tool_calls", self.max_tool_calls),
            ("retry_attempts", self.retry_attempts),
            ("circuit_failure_threshold", self.circuit_failure_threshold),
        ):
            if value <= 0:
                raise AgentError("invalid_config", f"{name} 必须大于 0", ErrorKind.VALIDATION)
        return self

    @classmethod
    def from_env(cls, prefix: str = "AGENT_NOTES_") -> "RuntimeConfig":
        def get(name: str, default: str) -> str:
            return os.environ.get(prefix + name, default)

        flag = get("REQUIRE_APPROVAL_FOR_WRITES", "1").lower() not in {"0", "false", "no"}
        return cls(
            service_name=get("SERVICE_NAME", cls.service_name),
            model_name=get("MODEL_NAME", cls.model_name),
            request_timeout_s=_positive_float(get("REQUEST_TIMEOUT_S", str(cls.request_timeout_s)), "REQUEST_TIMEOUT_S"),
            max_steps=_positive_int(get("MAX_STEPS", str(cls.max_steps)), "MAX_STEPS"),
            max_tool_calls=_positive_int(get("MAX_TOOL_CALLS", str(cls.max_tool_calls)), "MAX_TOOL_CALLS"),
            retry_attempts=_positive_int(get("RETRY_ATTEMPTS", str(cls.retry_attempts)), "RETRY_ATTEMPTS"),
            retry_total_timeout_s=_positive_float(get("RETRY_TOTAL_TIMEOUT_S", str(cls.retry_total_timeout_s)), "RETRY_TOTAL_TIMEOUT_S"),
            circuit_failure_threshold=_positive_int(get("CIRCUIT_FAILURE_THRESHOLD", str(cls.circuit_failure_threshold)), "CIRCUIT_FAILURE_THRESHOLD"),
            circuit_recovery_s=_positive_float(get("CIRCUIT_RECOVERY_S", str(cls.circuit_recovery_s)), "CIRCUIT_RECOVERY_S"),
            require_approval_for_writes=flag,
        ).validate()
