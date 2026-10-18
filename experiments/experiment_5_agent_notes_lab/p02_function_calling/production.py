"""Production-shaped function-calling boundary with validation and approval."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from common.config import RuntimeConfig
from common.errors import AgentError, ErrorKind, as_agent_error
from common.observability import Metrics
from common.resilience import RetryPolicy, retry_call, call_with_timeout
from common.security import ActionDescriptor, ToolPolicy
from common.storage import IdempotencyStore
from common.tools import validate_arguments


@dataclass
class FunctionCallResult:
    ok: bool
    content: str = ""
    error: dict[str, Any] = field(default_factory=dict)
    attempts: int = 0
    replayed: bool = False


class ProductionFunctionCaller:
    def __init__(self, model: Any, registry: Any, *, config: RuntimeConfig | None = None, idempotency_path: str | Path = "workspace/function-idempotency.jsonl"):
        self.model = model
        self.registry = registry
        self.config = (config or RuntimeConfig()).validate()
        self.policy = ToolPolicy(self._descriptors(), require_approval_for_writes=self.config.require_approval_for_writes)
        self.idempotency = IdempotencyStore(idempotency_path)
        self.metrics = Metrics()

    def _descriptors(self) -> list[ActionDescriptor]:
        tools = getattr(self.registry, "_tools", {})
        return [
            ActionDescriptor(
                name=name,
                read_only=bool(tool.annotations.get("read_only", True)),
                destructive=bool(tool.annotations.get("destructive", False)),
                external_side_effect=bool(tool.annotations.get("open_world", False)),
                idempotent=bool(tool.annotations.get("idempotent", False)),
            )
            for name, tool in tools.items()
        ]

    def call(self, schema: dict[str, Any], prompt: str, *, approval_token: str | None = None, idempotency_key: str | None = None) -> FunctionCallResult:
        request_key = idempotency_key or hashlib.sha256((json.dumps(schema, sort_keys=True) + prompt).encode()).hexdigest()
        request_hash = hashlib.sha256((json.dumps(schema, sort_keys=True) + prompt).encode()).hexdigest()
        status, cached = self.idempotency.begin(request_key, request_hash=request_hash)
        if status == "done":
            if isinstance(cached, dict) and cached.get("ok") is False:
                return FunctionCallResult(False, error=cached.get("error", {}), replayed=True)
            return FunctionCallResult(True, str(cached.get("content", cached) if isinstance(cached, dict) else cached), replayed=True)
        if status == "in_progress":
            return FunctionCallResult(False, error={"code": "request_in_progress", "message": "同一个请求正在执行或等待对账", "retryable": True}, replayed=True)
        attempts = 0
        try:
            def operation() -> str:
                nonlocal attempts
                attempts += 1
                response = call_with_timeout(lambda: self.model.chat([{"role": "user", "content": prompt}], [schema]), self.config.request_timeout_s)
                if len(response.tool_calls) != 1:
                    raise AgentError("invalid_model_output", "模型必须返回且只能返回一个工具调用", ErrorKind.VALIDATION, False)
                call = response.tool_calls[0]
                params = schema.get("function", {}).get("parameters", {})
                cleaned, missing, bad = validate_arguments(params, call.arguments)
                if missing or bad:
                    raise AgentError("invalid_arguments", "工具参数未通过 schema 校验", ErrorKind.VALIDATION, False, details={"missing": missing, "bad": bad})
                self.policy.authorize(call.name, approval_token=approval_token)
                result = self.registry.call(call.name, cleaned)
                if not result.ok:
                    raise AgentError("tool_failed", result.error.get("message", "工具执行失败"), ErrorKind.TRANSIENT if result.error.get("retryable") else ErrorKind.INTERNAL, bool(result.error.get("retryable")), details=result.error)
                return result.content

            planned_action = self.policy.descriptor(str(schema.get("function", {}).get("name", "")))
            max_attempts = self.config.retry_attempts if planned_action and (planned_action.read_only or planned_action.idempotent) else 1
            value = retry_call(operation, policy=RetryPolicy(max_attempts=max_attempts, total_timeout_s=self.config.retry_total_timeout_s, jitter_ratio=0.0))
            self.idempotency.complete(request_key, {"ok": True, "content": value}, request_hash=request_hash)
            self.metrics.inc("function_call.success")
            return FunctionCallResult(True, value, attempts=attempts)
        except BaseException as exc:
            error = as_agent_error(exc)
            if error.kind not in {ErrorKind.TIMEOUT, ErrorKind.TRANSIENT, ErrorKind.CONFLICT}:
                try:
                    self.idempotency.complete(request_key, {"ok": False, "error": error.safe_dict()}, request_hash=request_hash)
                except AgentError:
                    pass
            self.metrics.inc("function_call.failure")
            return FunctionCallResult(False, error=error.safe_dict(), attempts=attempts)
