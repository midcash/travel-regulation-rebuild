"""Durable memory and bounded context assembly."""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from common.config import RuntimeConfig
from common.errors import AgentError, ErrorKind
from common.llm import estimate_tokens
from common.storage import JsonlStore


@dataclass(frozen=True)
class MemoryRecord:
    key: str
    text: str
    scope: str = "user"
    importance: int = 1
    created_at: float = 0.0
    source: str = "user"

    def to_event(self, version: int) -> dict[str, Any]:
        return {"type": "memory", "version": version, "key": self.key, "text": self.text, "scope": self.scope, "importance": self.importance, "created_at": self.created_at or time.time(), "source": self.source}


class ProductionMemory:
    def __init__(self, path: str | Path = "workspace/memory-events.jsonl"):
        self.store = JsonlStore(path)
        self._records: dict[str, MemoryRecord] = {}
        self._version = 0
        for event in self.store.read():
            if event.get("type") == "memory" and event.get("key"):
                self._version = max(self._version, int(event.get("version", 0)))
                self._records[str(event["key"])] = MemoryRecord(str(event["key"]), str(event.get("text", "")), str(event.get("scope", "user")), int(event.get("importance", 1)), float(event.get("created_at", 0)), str(event.get("source", "unknown")))

    def put(self, record: MemoryRecord) -> None:
        if not record.key.strip() or not record.text.strip() or len(record.text) > 4000:
            raise AgentError("invalid_memory", "记忆 key/text 不能为空且文本不能超过 4000 字符", ErrorKind.VALIDATION)
        if record.scope not in {"user", "session", "agent"}:
            raise AgentError("invalid_scope", "记忆 scope 不在允许范围内", ErrorKind.VALIDATION)
        self._version += 1
        self.store.append(record.to_event(self._version))
        self._records[record.key] = record

    def search(self, query: str, *, scope: str | None = None, limit: int = 5) -> list[MemoryRecord]:
        words = set(query.lower().split())
        candidates = [item for item in self._records.values() if scope is None or item.scope == scope]
        ranked = sorted(candidates, key=lambda item: (len(words & set(item.text.lower().split())) * item.importance, item.created_at), reverse=True)
        return ranked[: max(0, min(limit, 20))]


class ContextAssembler:
    def __init__(self, *, max_tokens: int = 1200):
        self.max_tokens = max_tokens

    def assemble(self, query: str, memories: list[MemoryRecord], recent_messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if self.max_tokens < 1:
            raise ValueError("max_tokens must be positive")
        query = query.strip()
        if not query:
            raise ValueError("query cannot be empty")
        # The current request is mandatory context.  A memory assembler that
        # can return history but silently drop the question is not safe.
        selected: list[dict[str, Any]] = []
        query_message = {"role": "user", "content": query[: self.max_tokens]}
        used = estimate_tokens(query_message["content"]) + 4
        for record in sorted(memories, key=lambda item: (item.importance, item.created_at), reverse=True):
            text = f"[{record.scope}:{record.key}] {record.text}"
            cost = estimate_tokens(text)
            if used + cost > self.max_tokens:
                continue
            selected.append({"role": "system", "content": text})
            used += cost
        history: list[dict[str, Any]] = []
        for message in reversed(recent_messages):
            cost = estimate_tokens(str(message.get("content", ""))) + 4
            if used + cost > self.max_tokens:
                break
            if message.get("content") == query:
                continue
            history.insert(0, message)
            used += cost
        return selected + history + [query_message]
