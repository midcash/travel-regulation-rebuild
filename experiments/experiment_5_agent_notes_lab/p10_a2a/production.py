"""A2A-style task client with capability checks and idempotent submission."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from pathlib import Path
from typing import Any

from common.errors import AgentError, ErrorKind
from common.storage import IdempotencyStore


class TaskState(str, Enum):
    SUBMITTED = "submitted"
    WORKING = "working"
    INPUT_REQUIRED = "input_required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


TERMINAL = {TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELED}


@dataclass
class RemoteTask:
    task_id: str
    agent_id: str
    state: TaskState = TaskState.SUBMITTED
    updates: list[str] | None = None

    def __post_init__(self) -> None:
        self.updates = self.updates or []


class ProductionA2AClient:
    def __init__(self, directory: Any, *, bearer_token: str, idempotency_path: str | Path = "workspace/a2a-idempotency.jsonl"):
        if not bearer_token:
            raise ValueError("bearer_token is required")
        self.directory = directory
        self.bearer_token = bearer_token
        self.idempotency = IdempotencyStore(idempotency_path)

    def submit(self, agent_id: str, text: str, *, message_id: str) -> RemoteTask:
        if not text.strip() or not message_id.strip():
            raise AgentError("invalid_request", "任务文本和 message_id 不能为空", ErrorKind.VALIDATION)
        agent = self._resolve_authenticated(agent_id)
        card = agent.card()
        if not card.security:
            raise AgentError("insecure_agent", "远端 Agent 未声明安全方案", ErrorKind.AUTHORIZATION)
        request_hash = hashlib.sha256(json.dumps({"agent_id": agent_id, "text": text}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        status, cached = self.idempotency.begin(message_id, request_hash=request_hash)
        if status == "done":
            return RemoteTask(str(cached["task_id"]), agent_id, TaskState(str(cached["state"])))
        if status == "in_progress":
            raise AgentError("request_in_progress", "同一个 message_id 正在提交或等待对账", ErrorKind.CONFLICT, True)
        task = agent.send_task(self._message(text, message_id), idempotent=True)
        value = {"task_id": task.id, "state": task.state}
        self.idempotency.complete(message_id, value, request_hash=request_hash)
        return RemoteTask(task.id, agent_id, TaskState(task.state))

    def _message(self, text: str, message_id: str) -> Any:
        from protocol import Message, Part
        return Message(role="user", parts=[Part.of_text(text)], message_id=message_id)

    def _resolve_authenticated(self, agent_id: str) -> Any:
        resolver = getattr(self.directory, "resolve_authenticated", None)
        if callable(resolver):
            return resolver(agent_id, self.bearer_token)
        agent = self.directory.resolve_by_id(agent_id)
        authenticate = getattr(agent, "authenticate", None)
        if not callable(authenticate) or not bool(authenticate(self.bearer_token)):
            raise AgentError("missing_transport_auth", "A2A 传输适配器未验证 bearer token", ErrorKind.AUTHORIZATION)
        return agent

    def cancel(self, task: RemoteTask) -> None:
        if task.state in TERMINAL:
            return
        agent = self._resolve_authenticated(task.agent_id)
        agent.cancel_task(task.task_id)
        task.state = TaskState.CANCELED
