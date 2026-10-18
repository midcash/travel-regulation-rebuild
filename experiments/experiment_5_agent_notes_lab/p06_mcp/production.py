"""MCP-like host boundary with authentication, policy and idempotency."""
from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from common.config import RuntimeConfig
from common.errors import AgentError, ErrorKind, as_agent_error
from common.resilience import call_with_timeout, RetryPolicy, retry_call
from common.security import ActionDescriptor, ToolPolicy
from common.storage import IdempotencyStore


@dataclass(frozen=True)
class RpcRequest:
    request_id: str
    method: str
    params: dict[str, Any]
    bearer_token: str = ""


@dataclass
class RpcResponse:
    request_id: str
    ok: bool
    result: Any = None
    error: dict[str, Any] | None = None
    replayed: bool = False


class ProductionMCPHost:
    def __init__(self, registry: Any, *, expected_token: str, config: RuntimeConfig | None = None, idempotency_path: str | Path = "workspace/mcp-idempotency.jsonl", approval_secret: str | None = None, approval_verifier: Callable[[str, str], bool] | None = None):
        if not expected_token:
            raise ValueError("expected_token is required")
        self.registry = registry
        self.expected_token = expected_token
        self.config = (config or RuntimeConfig()).validate()
        self.idempotency = IdempotencyStore(idempotency_path)
        actions = []
        for name, tool in getattr(registry, "_tools", {}).items():
            actions.append(ActionDescriptor(name, bool(tool.annotations.get("read_only", True)), bool(tool.annotations.get("destructive", False)), bool(tool.annotations.get("open_world", False))))
        self.policy = ToolPolicy(actions, require_approval_for_writes=self.config.require_approval_for_writes, approval_secret=approval_secret, approval_verifier=approval_verifier)

    def handle(self, request: RpcRequest, *, approval_token: str | None = None) -> RpcResponse:
        if request.bearer_token != self.expected_token:
            error = AgentError("unauthenticated", "鉴权失败", ErrorKind.AUTHORIZATION, False, status=401)
            return RpcResponse(request.request_id, False, error=error.safe_dict())
        if request.method != "tools/call":
            error = AgentError("method_not_found", "不支持的 RPC 方法", ErrorKind.NOT_FOUND, False, status=404)
            return RpcResponse(request.request_id, False, error=error.safe_dict())
        name = str(request.params.get("name", ""))
        arguments = request.params.get("arguments", {})
        key = request.request_id
        try:
            action = self.policy.authorize(name, approval_token=approval_token)
            request_hash = hashlib.sha256(json.dumps({"method": request.method, "name": name, "arguments": arguments}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            status, cached = self.idempotency.begin(key, request_hash=request_hash)
            if status == "done":
                if isinstance(cached, dict) and cached.get("ok") is False:
                    return RpcResponse(key, False, error=cached.get("error"), replayed=True)
                return RpcResponse(key, True, cached.get("result") if isinstance(cached, dict) else cached, replayed=True)
            if status == "in_progress":
                error = AgentError("request_in_progress", "同一个 request_id 正在执行或等待对账，拒绝重复副作用", ErrorKind.CONFLICT, True)
                return RpcResponse(key, False, error=error.safe_dict(), replayed=True)

            def reconcile(raw_result: Any | None, late_error: BaseException | None) -> None:
                if late_error is not None:
                    error = as_agent_error(late_error)
                    self.idempotency.complete(key, {"ok": False, "error": error.safe_dict()}, request_hash=request_hash)
                    return
                if raw_result is not None and raw_result.ok:
                    late_value = {"content": raw_result.content, "structured": raw_result.structured}
                    self.idempotency.complete(key, {"ok": True, "result": late_value}, request_hash=request_hash)
                else:
                    detail = (raw_result.error if raw_result is not None else {}) or {}
                    error = AgentError("tool_failed", detail.get("message", "工具失败"), ErrorKind.INTERNAL, False, details=detail)
                    self.idempotency.complete(key, {"ok": False, "error": error.safe_dict()}, request_hash=request_hash)

            def operation() -> dict[str, Any]:
                result = call_with_timeout(lambda: self.registry.call(name, arguments), self.config.request_timeout_s, on_late_result=reconcile)
                if not result.ok:
                    kind = ErrorKind.TRANSIENT if result.error.get("retryable") else ErrorKind.INTERNAL
                    raise AgentError("tool_failed", result.error.get("message", "工具失败"), kind, bool(result.error.get("retryable")), details=result.error)
                return {"content": result.content, "structured": result.structured}

            # A timeout is ambiguous for a side-effecting tool.  Do not issue
            # a second attempt unless the tool is explicitly idempotent; the
            # request claim protects competing callers, not this local retry
            # loop.
            max_attempts = self.config.retry_attempts if action.read_only or action.idempotent else 1
            value = retry_call(operation, policy=RetryPolicy(max_attempts=max_attempts, total_timeout_s=self.config.retry_total_timeout_s, jitter_ratio=0.0))
            self.idempotency.complete(key, {"ok": True, "result": value}, request_hash=request_hash)
            return RpcResponse(key, True, value)
        except BaseException as exc:
            error = as_agent_error(exc)
            # A timeout/transient error may have happened after the side
            # effect started, so leave the claim in-progress for reconciliation
            # instead of allowing a blind second execution.
            if error.kind not in {ErrorKind.TIMEOUT, ErrorKind.TRANSIENT, ErrorKind.CONFLICT}:
                try:
                    self.idempotency.complete(key, {"ok": False, "error": error.safe_dict()})
                except AgentError:
                    pass
            return RpcResponse(key, False, error=error.safe_dict())
