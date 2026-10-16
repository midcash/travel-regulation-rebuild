"""OpenClaw-inspired production boundary: events, sessions, tools and sandbox."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from common.errors import AgentError, ErrorKind, as_agent_error
from common.resilience import Bulkhead, call_with_timeout
from common.security import ActionDescriptor, ToolPolicy
from common.storage import IdempotencyStore, JsonlStore


@dataclass(frozen=True)
class RuntimeEvent:
    event_id: str
    session_id: str
    kind: str
    payload: dict[str, Any]


class ProductionOpenClawRuntime:
    def __init__(self, root: str | Path = "workspace/openclaw-runtime", *, tool_timeout_s: float = 10.0, approval_secret: str | None = None, approval_verifier: Callable[[str, str], bool] | None = None):
        root = Path(root)
        self.events = JsonlStore(root / "events.jsonl")
        self.idempotency = IdempotencyStore(root / "idempotency.jsonl")
        if tool_timeout_s <= 0:
            raise ValueError("tool_timeout_s must be positive")
        self.tool_timeout_s = tool_timeout_s
        self.bulkhead = Bulkhead("openclaw-tools", 8)
        self.approval_secret = approval_secret
        self.approval_verifier = approval_verifier
        self.policy = ToolPolicy([], require_approval_for_writes=True, approval_secret=approval_secret, approval_verifier=approval_verifier)
        self.tools: dict[str, tuple[Callable[..., Any], ActionDescriptor]] = {}

    def register_tool(self, name: str, handler: Callable[..., Any], descriptor: ActionDescriptor) -> None:
        if name in self.tools:
            raise AgentError("duplicate_tool", f"工具 {name!r} 已注册", ErrorKind.CONFLICT)
        self.tools[name] = (handler, descriptor)
        self.policy = ToolPolicy([item[1] for item in self.tools.values()], approval_secret=self.approval_secret, approval_verifier=self.approval_verifier)

    def ingest(self, event: RuntimeEvent) -> None:
        self.events.append({"event_id": event.event_id, "session_id": event.session_id, "kind": event.kind, "payload": event.payload})

    def call_tool(self, session_id: str, name: str, args: dict[str, Any], *, request_id: str, approval_token: str | None = None) -> Any:
        entry = self.tools.get(name)
        if entry is None:
            raise AgentError("tool_not_found", f"工具 {name!r} 不存在", ErrorKind.NOT_FOUND)
        self.policy.authorize(name, approval_token=approval_token)
        request_hash = hashlib.sha256(json.dumps({"tool": name, "args": args}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        status, cached = self.idempotency.begin(request_id, request_hash=request_hash)
        if status == "done":
            if isinstance(cached, dict) and cached.get("ok") is False:
                raise AgentError("replayed_failure", "重放到之前已记录的工具失败", ErrorKind.INTERNAL, details=cached.get("error", {}))
            return cached.get("result") if isinstance(cached, dict) else cached
        if status == "in_progress":
            raise AgentError("request_in_progress", "同一个 request_id 正在执行或等待对账，拒绝重复副作用", ErrorKind.CONFLICT, True)
        handler = entry[0]

        def reconcile(value: Any | None, late_error: BaseException | None) -> None:
            """Close the claim when a timed-out adapter eventually finishes."""
            if late_error is None:
                self.idempotency.complete(request_id, {"ok": True, "result": value}, request_hash=request_hash)
                self.ingest(RuntimeEvent(request_id, session_id, "tool.reconciled", {"tool": name, "ok": True}))
                return
            error = as_agent_error(late_error)
            self.idempotency.complete(request_id, {"ok": False, "error": error.safe_dict()}, request_hash=request_hash)
            self.ingest(RuntimeEvent(request_id, session_id, "tool.reconciled", {"tool": name, "ok": False, "error": error.safe_dict()}))

        try:
            with self.bulkhead.slot():
                value = call_with_timeout(lambda: handler(**args), self.tool_timeout_s, on_late_result=reconcile)
            self.idempotency.complete(request_id, {"ok": True, "result": value}, request_hash=request_hash)
            self.ingest(RuntimeEvent(request_id, session_id, "tool.completed", {"tool": name, "ok": True}))
            return value
        except BaseException as exc:
            error = as_agent_error(exc)
            if error.kind not in {ErrorKind.TIMEOUT, ErrorKind.TRANSIENT, ErrorKind.CONFLICT}:
                try:
                    self.idempotency.complete(request_id, {"ok": False, "error": error.safe_dict()}, request_hash=request_hash)
                except AgentError:
                    pass
            self.ingest(RuntimeEvent(request_id, session_id, "tool.failed", {"tool": name, "error": error.safe_dict()}))
            raise error
