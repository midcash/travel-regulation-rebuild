"""Durable, dependency-free stores used by the production learning code."""
from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from common.errors import AgentError, ErrorKind


class JsonlStore:
    """Single-process append-only JSONL store.

    Each append flushes and fsyncs.  The lock protects threads in one worker;
    multi-replica production code should replace this boundary with a database
    or queue that provides a cross-process uniqueness/transaction guarantee.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def append(self, event: dict[str, Any]) -> None:
        line = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        with self._lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())

    def read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        events: list[dict[str, Any]] = []
        with self._lock, self.path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"损坏的 JSONL 事件，第 {line_number} 行") from exc
                if not isinstance(item, dict):
                    raise ValueError(f"JSONL 事件必须是 object，第 {line_number} 行")
                events.append(item)
        return events


class IdempotencyStore:
    """Durable first-write-wins result cache for request/message ids.

    In addition to the in-process lock, ``begin`` takes an OS file lock and
    reloads the journal before deciding.  That gives the examples a genuine
    cross-process claim on local files.  In a multi-host deployment the same
    contract should be implemented by a database unique constraint or a
    Redis ``SET NX``/Lua transaction.
    """

    def __init__(self, path: str | Path):
        self._events = JsonlStore(path)
        self._lock = threading.RLock()
        self._lock_path = self._events.path.with_name(self._events.path.name + ".lock")
        self._values: dict[str, Any] = {}
        self._records: dict[str, dict[str, Any]] = {}
        self._reload()

    @contextmanager
    def _process_lock(self) -> Iterator[None]:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock_path.open("a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _reload(self) -> None:
        self._values.clear()
        self._records.clear()
        for event in self._events.read():
            if event.get("type") == "idempotency" and isinstance(event.get("key"), str):
                self._values[event["key"]] = event.get("value")
                self._records[event["key"]] = {"status": "done", "value": event.get("value"), "request_hash": event.get("request_hash", "")}
            elif event.get("type") in {"idempotency.begin", "idempotency.complete"} and isinstance(event.get("key"), str):
                key = event["key"]
                record = self._records.setdefault(key, {})
                if event["type"] == "idempotency.begin":
                    record.update({"status": "in_progress", "request_hash": event.get("request_hash", "")})
                else:
                    record.update({"status": "done", "value": event.get("value"), "request_hash": event.get("request_hash", record.get("request_hash", ""))})
                    self._values[key] = event.get("value")

    def get(self, key: str) -> Any | None:
        with self._lock, self._process_lock():
            self._reload()
            return self._values.get(key)

    def begin(self, key: str, *, request_hash: str = "") -> tuple[str, Any | None]:
        """Atomically claim a request before executing its side effect.

        ``in_progress`` is intentionally not auto-reclaimed: after a timeout
        the caller cannot know whether the remote side effect happened.  A
        real service resolves this state with a durable workflow/outbox or an
        operator policy, never by blindly executing the side effect again.
        """
        if not key.strip():
            raise ValueError("idempotency key cannot be empty")
        with self._lock, self._process_lock():
            self._reload()
            record = self._records.get(key)
            if record is not None:
                old_hash = str(record.get("request_hash", ""))
                if old_hash and request_hash and old_hash != request_hash:
                    raise AgentError("idempotency_conflict", "同一个幂等键对应了不同请求", ErrorKind.CONFLICT)
                return str(record.get("status", "in_progress")), record.get("value")
            self._events.append({"type": "idempotency.begin", "key": key, "request_hash": request_hash})
            self._records[key] = {"status": "in_progress", "request_hash": request_hash}
            return "claimed", None

    def complete(self, key: str, value: Any, *, request_hash: str = "") -> None:
        with self._lock, self._process_lock():
            self._reload()
            record = self._records.get(key)
            if record is None:
                raise AgentError("idempotency_missing", "幂等请求尚未 claim", ErrorKind.CONFLICT)
            if record.get("status") == "done":
                return
            old_hash = str(record.get("request_hash", ""))
            if old_hash and request_hash and old_hash != request_hash:
                raise AgentError("idempotency_conflict", "同一个幂等键对应了不同请求", ErrorKind.CONFLICT)
            self._events.append({"type": "idempotency.complete", "key": key, "request_hash": request_hash or old_hash, "value": value})
            record.update({"status": "done", "value": value})
            self._values[key] = value

    def put_if_absent(self, key: str, value: Any) -> tuple[Any | None, bool]:
        if not key.strip():
            raise ValueError("idempotency key cannot be empty")
        with self._lock:
            status, existing = self.begin(key)
            if status == "done":
                return existing, False
            if status == "in_progress":
                return None, False
            self.complete(key, value)
            return value, True
