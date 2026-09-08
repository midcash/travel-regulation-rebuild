"""Local durable telemetry. Raw prompts, tool content, and reasoning are not persisted."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import time
import uuid


def utcnow():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path="artifacts/observability.sqlite3"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS runs (
                    trace_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS spans (
                    span_id TEXT PRIMARY KEY, trace_id TEXT NOT NULL, started_at TEXT NOT NULL,
                    payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS spans_trace ON spans(trace_id);
                CREATE TABLE IF NOT EXISTS evaluations (
                    id TEXT PRIMARY KEY, started_at TEXT NOT NULL, payload TEXT NOT NULL);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(str(self.path), timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def save_run(self, item):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO runs VALUES (?, ?, ?)",
                       (item["trace_id"], item["started_at"], json.dumps(item, ensure_ascii=False)))

    def save_span(self, item):
        with self.connect() as db:
            db.execute("INSERT INTO spans VALUES (?, ?, ?, ?)",
                       (item["span_id"], item["trace_id"], item["started_at"], json.dumps(item)))

    def save_evaluation(self, item):
        with self.connect() as db:
            db.execute("INSERT INTO evaluations VALUES (?, ?, ?)",
                       (item["id"], item["started_at"], json.dumps(item, ensure_ascii=False)))

    def list_runs(self, limit=200):
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute(
                "SELECT payload FROM runs ORDER BY started_at DESC LIMIT ?", (limit,))]

    def get_run(self, trace_id):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM runs WHERE trace_id=?", (trace_id,)).fetchone()
            if not row:
                return None
            result = json.loads(row[0])
            result["spans"] = [json.loads(x[0]) for x in db.execute(
                "SELECT payload FROM spans WHERE trace_id=? ORDER BY started_at", (trace_id,))]
            return result

    def list_evaluations(self):
        with self.connect() as db:
            return [json.loads(x[0]) for x in db.execute(
                "SELECT payload FROM evaluations ORDER BY started_at DESC LIMIT 30")]


class Tracer:
    def __init__(self, store, otel=None):
        self.store = store
        self.otel = otel
        self.trace_id = uuid.uuid4().hex
        self.stack = []

    @contextmanager
    def span(self, name, **attributes):
        """
            职责：创建用于日志服务的上下文管理器
                1. 创建记录、记录开始时间、放入队列
                2.
        """
        record = {"trace_id": self.trace_id, "span_id": uuid.uuid4().hex[:16],
                  "parent_span_id": self.stack[-1] if self.stack else None,
                  "name": name, "started_at": utcnow(), "status": "ok", "attributes": attributes}
        start = time.perf_counter()
        self.stack.append(record["span_id"])
        from contextlib import nullcontext
        context = self.otel.start_as_current_span(name, record_exception=False,
                  set_status_on_exception=False) if self.otel else nullcontext(None)
        try:
            with context as external:
                try:
                    yield record["attributes"]
                except Exception as exc:
                    record["status"] = "error"
                    record["attributes"]["error.type"] = type(exc).__name__
                    if getattr(exc, "reason", None) in {"steps", "tool_calls", "tokens", "deadline"}:
                        record["attributes"]["budget.reason"] = exc.reason
                    if type(getattr(exc, "http_status", None)) is int:
                        record["attributes"]["http.status_code"] = exc.http_status
                    raise
                finally:
                    if external:
                        external.set_attribute("travel.local_trace_id", self.trace_id)
                        external.set_attribute("travel.local_span_id", record["span_id"])
                        for key, value in record["attributes"].items():
                            if isinstance(value, (str, bool, int, float)):
                                external.set_attribute(key, value)
                        if record["status"] == "error":
                            from opentelemetry.trace import Status, StatusCode
                            external.set_status(Status(StatusCode.ERROR))
        finally:
            self.stack.pop()
            record["duration_ms"] = round((time.perf_counter() - start) * 1000, 3)
            self.store.save_span(record)


def make_otel(endpoint):
    """Explicit optional exporter; no auto-instrumentation captures payloads."""
    if not endpoint:
        return None, None
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    provider = TracerProvider(resource=Resource.create({"service.name": "travel-agent-lab"}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, timeout=5)))
    # 这里的 BatchSpanProcessor 负责收集已结束的 span、批量处理；OTLPSpanExporter 负责通过 HTTP 将数据发送到指定的 endpoint。所以你没有在 span() 中看到显式的发送或返回代码——这些由 SDK 接手。
    return provider.get_tracer("travel-agent", "0.1.0"), provider
