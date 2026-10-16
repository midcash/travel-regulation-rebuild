"""Pi-inspired session tree with append-only events and safe compaction."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from common.errors import AgentError, ErrorKind
from common.storage import JsonlStore


@dataclass(frozen=True)
class SessionEvent:
    event_id: str
    parent_id: str | None
    role: str
    content: str


class ProductionSessionTree:
    def __init__(self, path: str | Path = "workspace/session-events.jsonl"):
        self.store = JsonlStore(path)
        self.events: dict[str, SessionEvent] = {}
        self.children: dict[str | None, list[str]] = {}
        self._replay()

    def _replay(self) -> None:
        for raw in self.store.read():
            if raw.get("type") != "message":
                continue
            event = SessionEvent(str(raw.get("event_id", "")), raw.get("parent_id"), str(raw.get("role", "")), str(raw.get("content", "")))
            if event.event_id and event.event_id not in self.events:
                self.events[event.event_id] = event
                self.children.setdefault(event.parent_id, []).append(event.event_id)

    def append(self, event: SessionEvent) -> None:
        if event.event_id in self.events:
            return
        if event.parent_id is not None and event.parent_id not in self.events:
            raise AgentError("missing_parent", "session event 的 parent 不存在", ErrorKind.CONFLICT)
        if event.role not in {"user", "assistant", "tool", "system"} or not event.content.strip():
            raise AgentError("invalid_event", "session event 角色或内容非法", ErrorKind.VALIDATION)
        self.store.append({"type": "message", "event_id": event.event_id, "parent_id": event.parent_id, "role": event.role, "content": event.content})
        self.events[event.event_id] = event
        self.children.setdefault(event.parent_id, []).append(event.event_id)

    def branch(self, event_id: str, new_event_id: str, role: str, content: str) -> SessionEvent:
        if event_id not in self.events:
            raise AgentError("event_not_found", "分支起点不存在", ErrorKind.NOT_FOUND)
        event = SessionEvent(new_event_id, event_id, role, content)
        self.append(event)
        return event

    def lineage(self, leaf_id: str) -> list[SessionEvent]:
        chain: list[SessionEvent] = []
        current = leaf_id
        while current:
            event = self.events.get(current)
            if event is None:
                raise AgentError("event_not_found", "lineage 中存在未知事件", ErrorKind.CONFLICT)
            chain.append(event)
            current = event.parent_id or ""
        return list(reversed(chain))

    def compact(self, leaf_id: str, keep: int = 12) -> list[SessionEvent]:
        if keep < 1:
            raise ValueError("keep must be positive")
        return self.lineage(leaf_id)[-keep:]
