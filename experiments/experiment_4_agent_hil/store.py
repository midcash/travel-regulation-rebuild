"""实验四的存储层：三张表 + 短事务。

与 exp3 runtime.py 的 HilStore 的差别，全部来自"要存的东西变了"：

1. runs.state_json 装的不再是 selected/details/plan 这种小状态，
   而是完整对话历史 + 工具内部计数器，所以额外拆出可查询的列。
2. 写方法一律要求调用方传入 db（即必须在 transaction() 里），
   读方法可以不传。这样"哪些操作属于同一个原子提交"在调用点一眼可见——
   respond 里"消费 + 喂回 + 复位"必须是同一个事务，靠的就是这个签名。
3. 事务只包住数据库操作，绝不包住模型调用和工具执行。
   exp3 README 第 50 行自己写下的取舍（"接入真实模型或网络工具时，
   不应持有数据库写锁等待网络"）在这里兑现。
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import sqlite3
import uuid

from .snapshot import decode, encode

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    step INTEGER NOT NULL,
    human_requests INTEGER NOT NULL,
    snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS human_interactions (
    interaction_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    state_version INTEGER NOT NULL,
    tool_call_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    response_json TEXT,
    response_id TEXT UNIQUE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT
);
CREATE INDEX IF NOT EXISTS interactions_run ON human_interactions(run_id);
CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class StoreError(Exception):
    pass


def utcnow():
    return datetime.now(timezone.utc).isoformat()


class AgentHilStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as db:
            db.executescript(SCHEMA)

    # ---------------- 事务 ----------------

    @contextmanager
    def transaction(self):
        """一个短事务。BEGIN IMMEDIATE 立刻拿写锁，避免读改写竞争。

        调用方必须在 with 块内完成全部数据库操作；块内不得调模型或执行工具。
        """
        db = sqlite3.connect(str(self.path), timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("BEGIN IMMEDIATE")
            yield db
        except Exception:
            db.rollback()
            raise
        else:
            db.commit()
        finally:
            db.close()

    @contextmanager
    def _db(self, db=None):
        """读方法复用调用方的事务连接；没有就自己开一个只读用的连接。"""
        if db is not None:
            yield db
            return
        conn = sqlite3.connect(str(self.path), timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    # ---------------- runs ----------------

    def create_run(self, db, snapshot):
        now = utcnow()
        db.execute("INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (snapshot["run_id"], snapshot["status"], snapshot["step"],
                    snapshot["human_requests"], encode(snapshot), now, now))
        self.event(db, snapshot["run_id"], "run_created", {"status": snapshot["status"]})

    def save_snapshot(self, db, snapshot, event_type, payload):
        """落库 + 记事件。永远同一个事务，所以事件流不会和快照脱节。"""
        updated = db.execute(
            "UPDATE runs SET status=?, step=?, human_requests=?, snapshot_json=?, updated_at=? "
            "WHERE run_id=?",
            (snapshot["status"], snapshot["step"], snapshot["human_requests"],
             encode(snapshot), utcnow(), snapshot["run_id"]))
        if updated.rowcount != 1:
            raise StoreError(f"run not found: {snapshot['run_id']}")
        self.event(db, snapshot["run_id"], event_type, payload)

    def load_snapshot(self, run_id, db=None):
        with self._db(db) as conn:
            row = conn.execute("SELECT snapshot_json FROM runs WHERE run_id=?",
                               (run_id,)).fetchone()
        if row is None:
            raise StoreError(f"run not found: {run_id}")
        return decode(row["snapshot_json"])   # decode 内含不变量自检

    def list_runs(self, limit=100):
        with self._db() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT run_id, status, step, human_requests, created_at, updated_at "
                "FROM runs ORDER BY created_at DESC LIMIT ?", (limit,))]

    # ---------------- human_interactions ----------------

    def create_interaction(self, db, run_id, kind, state_version, tool_call_id,
                           payload, ttl_minutes):
        interaction = {
            "interaction_id": uuid.uuid4().hex, "run_id": run_id, "kind": kind,
            "status": "pending", "state_version": state_version,
            "tool_call_id": tool_call_id, "payload": payload,
            "created_at": utcnow(),
            "expires_at": (datetime.now(timezone.utc)
                           + timedelta(minutes=ttl_minutes)).isoformat(),
        }
        db.execute(
            "INSERT INTO human_interactions (interaction_id, run_id, kind, status, "
            "state_version, tool_call_id, payload_json, response_json, response_id, "
            "created_at, expires_at, consumed_at) "
            "VALUES (?, ?, ?, 'pending', ?, ?, ?, NULL, NULL, ?, ?, NULL)",
            (interaction["interaction_id"], run_id, kind, state_version, tool_call_id,
             json.dumps(payload, ensure_ascii=False), interaction["created_at"],
             interaction["expires_at"]))
        return interaction

    def get_interaction(self, interaction_id, db=None):
        with self._db(db) as conn:
            row = conn.execute("SELECT * FROM human_interactions WHERE interaction_id=?",
                               (interaction_id,)).fetchone()
        if row is None:
            raise StoreError(f"interaction not found: {interaction_id}")
        return interaction_from_row(row)

    def latest_interaction(self, run_id, db=None):
        with self._db(db) as conn:
            row = conn.execute(
                "SELECT * FROM human_interactions WHERE run_id=? "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1", (run_id,)).fetchone()
        return None if row is None else interaction_from_row(row)

    def consume_interaction(self, db, interaction_id, response_id, response):
        """把人工回复写进去。必须在调用方的事务里跑——它和"喂回 tool 消息"、
        "状态复位"必须一起提交，只做一半就会产生永远等不到结果的运行，
        或两条同 tool_call_id 的回复（后者会被快照不变量直接拦住）。"""
        row = db.execute("SELECT * FROM human_interactions WHERE interaction_id=?",
                         (interaction_id,)).fetchone()
        if row is None:
            raise StoreError(f"interaction not found: {interaction_id}")
        current = interaction_from_row(row)
        if current["status"] == "consumed":
            if current["response_id"] == response_id:
                return current          # 幂等：同一 response_id 重复提交
            raise StoreError("interaction already consumed")
        if current["status"] != "pending":
            raise StoreError(f"interaction is {current['status']}")
        db.execute("UPDATE human_interactions SET status='consumed', response_json=?, "
                   "response_id=?, consumed_at=? WHERE interaction_id=? AND status='pending'",
                   (json.dumps(response, ensure_ascii=False), response_id,
                    utcnow(), interaction_id))
        self.event(db, current["run_id"], "human_responded",
                   {"interaction_id": interaction_id, "response_id": response_id})
        current.update({"status": "consumed", "response": response,
                        "response_id": response_id})
        return current

    def expire_interaction(self, db, interaction_id):
        db.execute("UPDATE human_interactions SET status='expired' "
                   "WHERE interaction_id=? AND status='pending'", (interaction_id,))
        self.event(db, self.get_interaction(interaction_id, db)["run_id"],
                   "human_expired", {"interaction_id": interaction_id})

    # ---------------- events ----------------

    def event(self, db, run_id, event_type, payload):
        db.execute("INSERT INTO events (run_id, event_type, payload_json, created_at) "
                   "VALUES (?, ?, ?, ?)",
                   (run_id, event_type, json.dumps(payload, ensure_ascii=False), utcnow()))

    def list_events(self, run_id):
        with self._db() as conn:
            rows = conn.execute("SELECT event_type, payload_json, created_at FROM events "
                                "WHERE run_id=? ORDER BY event_id", (run_id,)).fetchall()
        return [{"type": row["event_type"],
                 "payload": json.loads(row["payload_json"]),
                 "created_at": row["created_at"]} for row in rows]


def interaction_from_row(row):
    result = {
        "interaction_id": row["interaction_id"], "run_id": row["run_id"],
        "kind": row["kind"], "status": row["status"],
        "state_version": row["state_version"], "tool_call_id": row["tool_call_id"],
        "payload": json.loads(row["payload_json"]),
        "created_at": row["created_at"], "expires_at": row["expires_at"],
        "response_id": row["response_id"],
    }
    if row["response_json"]:
        result["response"] = json.loads(row["response_json"])
    return result
