"""OpenClaw 结构原型：触发层 -> Gateway -> Agent -> 会话与记忆。

这个文件刻意不接真实网络。重点是把第 11 章的职责边界落成几个可调试对象：
Gateway 只负责认证、归一化、路由；Agent 才负责推理和工具执行。
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Callable


TRIGGER_KINDS = {"message", "heartbeat", "cron", "hook", "webhook"}


@dataclass(frozen=True)
class Event:
    id: str
    kind: str
    session_id: str
    channel: str
    payload: dict[str, Any]


class TriggerLayer:
    """五种信号源的薄封装；它只产生事件，不执行 Agent。"""

    def __init__(self) -> None:
        self._next_id = 1

    def emit(self, kind: str, session_id: str, payload: dict[str, Any], channel: str) -> Event:
        if kind not in TRIGGER_KINDS:
            raise ValueError(f"未知触发源: {kind}")
        event = Event(f"evt-{self._next_id:03d}", kind, session_id, channel, dict(payload))
        self._next_id += 1
        return event


class Gateway:
    """本地网关：认证、归一化和路由，绝不调用模型或工具。"""

    def __init__(self, valid_tokens: set[str]) -> None:
        self.valid_tokens = set(valid_tokens)
        self.inboxes: dict[str, list[Event]] = {}
        self.audit: list[str] = []

    def ingest(self, event: Event, token: str) -> bool:
        if token not in self.valid_tokens:
            self.audit.append(f"{event.id}: rejected(auth)")
            return False
        normalized = Event(
            id=event.id,
            kind=event.kind,
            session_id=event.session_id,
            channel=event.channel,
            payload={"text": str(event.payload.get("text", "")), **event.payload},
        )
        self.inboxes.setdefault(normalized.session_id, []).append(normalized)
        self.audit.append(f"{event.id}: routed({normalized.session_id})")
        return True

    def pop(self, session_id: str) -> Event | None:
        queue = self.inboxes.get(session_id, [])
        return queue.pop(0) if queue else None


@dataclass
class SessionTranscript:
    """示意 JSONL 会话：每次事件都追加，Agent 重启后可从这里恢复。"""

    entries: list[dict[str, Any]] = field(default_factory=list)

    def append(self, role: str, content: str, **extra: Any) -> None:
        self.entries.append({"role": role, "content": content, **extra})


@dataclass
class BootstrapFiles:
    """每次会话都注入的稳定身份/规则，不依赖对话历史。"""

    identity: str
    rules: str


class RetrievalIndex:
    """无依赖的关键词检索，展示 memory_search -> memory_get 两步形状。"""

    def __init__(self, files: dict[str, str]) -> None:
        self.files = dict(files)

    @staticmethod
    def _terms(text: str) -> set[str]:
        return set(re.findall(r"[\u4e00-\u9fff]+|[a-z0-9_]+", text.lower()))

    def search(self, query: str, top_k: int = 2) -> list[dict[str, Any]]:
        query_terms = self._terms(query)
        ranked: list[tuple[int, str, str]] = []
        for path, content in self.files.items():
            score = len(query_terms & self._terms(content))
            if score:
                ranked.append((score, path, content))
        ranked.sort(key=lambda row: (-row[0], row[1]))
        return [{"path": path, "score": score, "snippet": content[:100]} for score, path, content in ranked[:top_k]]

    def get(self, path: str) -> str:
        return self.files[path]


class SecureToolRunner:
    """工具权限边界：可用能力必须显式进入 allowlist。"""

    def __init__(self, index: RetrievalIndex) -> None:
        self.index = index
        self.allowlist = {"memory_search", "memory_get", "send_message"}

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in self.allowlist:
            return {"ok": False, "error": f"tool_forbidden:{name}"}
        if name == "memory_search":
            return {"ok": True, "items": self.index.search(str(arguments.get("query", "")))}
        if name == "memory_get":
            path = str(arguments.get("path", ""))
            if path not in self.index.files:
                return {"ok": False, "error": "memory_not_found"}
            return {"ok": True, "content": self.index.get(path)}
        return {"ok": True, "sent": str(arguments.get("text", ""))}


class OpenClawAgent:
    """一个极小的 Agent 层：根据事件类型决定是否使用记忆或回复渠道。"""

    def __init__(
        self,
        bootstrap: BootstrapFiles,
        transcript: SessionTranscript,
        tools: SecureToolRunner,
        policy: Callable[[Event, list[dict[str, Any]]], tuple[str, dict[str, Any]] | str],
    ) -> None:
        self.bootstrap = bootstrap
        self.transcript = transcript
        self.tools = tools
        self.policy = policy
        self.tool_calls = 0

    def handle(self, event: Event) -> dict[str, Any]:
        text = event.payload.get("text", "")
        self.transcript.append("user", text, event=event.kind, channel=event.channel)
        decision = self.policy(event, self.transcript.entries)
        if isinstance(decision, tuple):
            tool_name, arguments = decision
            result = self.tools.call(tool_name, arguments)
            self.tool_calls += 1
            self.transcript.append("tool", str(result), name=tool_name)
            answer = f"{tool_name} -> {result}"
        else:
            answer = decision
        self.transcript.append("assistant", answer)
        return {"event": event.id, "answer": answer, "tool_calls": self.tool_calls}


class OpenClawSystem:
    """把触发层、Gateway 和 Agent 串成事件驱动管道。"""

    def __init__(self, agent: OpenClawAgent, gateway: Gateway) -> None:
        self.agent = agent
        self.gateway = gateway

    def dispatch(self, event: Event, token: str) -> dict[str, Any]:
        if not self.gateway.ingest(event, token):
            return {"event": event.id, "status": "rejected"}
        routed = self.gateway.pop(event.session_id)
        if routed is None:  # 防御性分支，正常不会发生
            return {"event": event.id, "status": "lost"}
        return self.agent.handle(routed)
