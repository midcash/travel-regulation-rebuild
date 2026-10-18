"""SQLite store for Pi conversations: the single source of truth for the skeleton.

Holds conversations, messages, runs, Pi SDK entries, tool calls, evidence and a
per-conversation event log. Every write is one transaction that commits before the
caller forwards anything to the model or the page. Only ``pi_*`` tables are created;
other tables in the same file are never altered or dropped.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import uuid

# China has no DST; a fixed offset avoids needing tzdata on Windows.
SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")
ACTIVE_RUN = ("running", "cancelling")
FINAL_RUN = ("completed", "failed", "cancelled", "interrupted")
MAX_ID_LENGTH = 128


class StoreError(Exception):
    """Base class; ``code`` is a stable machine-readable reason for the API layer."""
    code = "store_error"


class NotFound(StoreError):
    code = "not_found"


class MessageConflict(StoreError):
    code = "message_conflict"


class ConversationBusy(StoreError):
    code = "conversation_busy"


class RunNotActive(StoreError):
    code = "run_not_active"


class ToolCallConflict(StoreError):
    code = "tool_call_conflict"


class ToolOutcomeUnknown(StoreError):
    """A call with this ID was sent but its result was never committed; use a new ID."""
    code = "tool_outcome_unknown"


SCHEMA = """
CREATE TABLE IF NOT EXISTS pi_conversations (
    id TEXT PRIMARY KEY,
    title TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pi_messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES pi_conversations(id),
    seq INTEGER NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    body TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    run_id TEXT,
    UNIQUE (conversation_id, seq));
CREATE TABLE IF NOT EXISTS pi_runs (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES pi_conversations(id),
    message_id TEXT NOT NULL REFERENCES pi_messages(id),
    status TEXT NOT NULL CHECK (status IN
        ('running', 'cancelling', 'completed', 'failed', 'cancelled', 'interrupted')),
    stop_reason TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    usage TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS pi_runs_one_active
    ON pi_runs(conversation_id) WHERE status IN ('running', 'cancelling');
CREATE TABLE IF NOT EXISTS pi_entries (
    conversation_id TEXT NOT NULL REFERENCES pi_conversations(id),
    seq INTEGER NOT NULL,
    id TEXT NOT NULL,
    body TEXT NOT NULL,
    PRIMARY KEY (conversation_id, seq),
    UNIQUE (conversation_id, id));
CREATE TABLE IF NOT EXISTS pi_tool_calls (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES pi_conversations(id),
    run_id TEXT NOT NULL REFERENCES pi_runs(id),
    name TEXT NOT NULL,
    args TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'complete', 'failed', 'discarded', 'unknown')),
    result TEXT,
    requested_at TEXT NOT NULL,
    completed_at TEXT);
CREATE TABLE IF NOT EXISTS pi_evidence (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES pi_conversations(id),
    tool_call_id TEXT NOT NULL REFERENCES pi_tool_calls(id),
    fetched_at TEXT NOT NULL,
    facts TEXT NOT NULL,
    raw TEXT);
CREATE TABLE IF NOT EXISTS pi_events (
    conversation_id TEXT NOT NULL REFERENCES pi_conversations(id),
    seq INTEGER NOT NULL,
    run_id TEXT,
    type TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (conversation_id, seq));
"""


def _dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _check_id(value, name):
    if not isinstance(value, str) or not value or len(value) > MAX_ID_LENGTH:
        raise ValueError(f"{name} must be a non-empty string of at most {MAX_ID_LENGTH} characters")


def _new_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex}"


class ConversationStore:
    """Thread-safe by construction: each operation opens and closes its own connection."""

    def __init__(self, path, clock=None):
        self.path = Path(path)
        self.clock = clock or (lambda: datetime.now(SHANGHAI))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(self.path), timeout=10)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
        finally:
            db.close()

    # -- time ---------------------------------------------------------------
    def now(self):
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return value.astimezone(SHANGHAI)

    def today(self):
        """Shanghai date re-read from the clock on every call (no cached snapshot)."""
        return self.now().date()

    def _stamp(self):
        return self.now().isoformat(timespec="microseconds")

    # -- plumbing -----------------------------------------------------------
    @contextmanager
    def _tx(self, write=True):
        db = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                yield db
            except BaseException:
                db.execute("ROLLBACK")
                raise
            else:
                db.execute("COMMIT")
        finally:
            db.close()

    @staticmethod
    def _conversation(db, conversation_id):
        row = db.execute("SELECT * FROM pi_conversations WHERE id=?", (conversation_id,)).fetchone()
        if row is None:
            raise NotFound("conversation not found")
        return row

    @staticmethod
    def _run(db, run_id):
        row = db.execute("SELECT * FROM pi_runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise NotFound("run not found")
        return row

    def _emit(self, db, conversation_id, kind, payload, run_id=None, stamp=None):
        seq = db.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM pi_events WHERE conversation_id=?",
                         (conversation_id,)).fetchone()[0]
        db.execute("INSERT INTO pi_events VALUES (?, ?, ?, ?, ?, ?)",
                   (conversation_id, seq, run_id, kind, _dump(payload), stamp or self._stamp()))
        return seq

    @staticmethod
    def _message(row):
        return {"id": row["id"], "conversation_id": row["conversation_id"], "seq": row["seq"],
                "role": row["role"], "text": row["body"], "captured_at": row["captured_at"],
                "run_id": row["run_id"]}

    @staticmethod
    def _run_dict(row):
        return {"id": row["id"], "conversation_id": row["conversation_id"],
                "message_id": row["message_id"], "status": row["status"],
                "stop_reason": row["stop_reason"], "started_at": row["started_at"],
                "ended_at": row["ended_at"],
                "usage": json.loads(row["usage"]) if row["usage"] else None}

    # -- conversations ------------------------------------------------------
    def create_conversation(self, title=None):
        conversation_id, stamp = _new_id("conv"), self._stamp()
        with self._tx() as db:
            db.execute("INSERT INTO pi_conversations VALUES (?, ?, ?, ?)",
                       (conversation_id, title, stamp, stamp))
            self._emit(db, conversation_id, "conversation_created", {"title": title}, stamp=stamp)
        return {"id": conversation_id, "title": title, "created_at": stamp, "updated_at": stamp}

    def list_conversations(self, limit=100):
        with self._tx(write=False) as db:
            rows = db.execute("SELECT * FROM pi_conversations ORDER BY updated_at DESC, id LIMIT ?",
                              (limit,)).fetchall()
        return [dict(row) for row in rows]

    def get_state(self, conversation_id):
        """Consistent snapshot plus the event cursor to subscribe from."""
        with self._tx(write=False) as db:
            conversation = dict(self._conversation(db, conversation_id))
            messages = [self._message(r) for r in db.execute(
                "SELECT * FROM pi_messages WHERE conversation_id=? ORDER BY seq", (conversation_id,))]
            active = db.execute("SELECT * FROM pi_runs WHERE conversation_id=? AND status IN (?, ?)",
                                (conversation_id, *ACTIVE_RUN)).fetchone()
            last_seq = db.execute("SELECT COALESCE(MAX(seq), 0) FROM pi_events WHERE conversation_id=?",
                                  (conversation_id,)).fetchone()[0]
        return {"conversation": conversation, "messages": messages,
                "active_run": self._run_dict(active) if active else None, "last_event_seq": last_seq}

    # -- messages and runs --------------------------------------------------
    def submit_message(self, conversation_id, message_id, text):
        """Store a user message and start its run atomically.

        Re-sending the same ID with the same text returns the original message and run
        (``created`` is False). The same ID with different text, or in another
        conversation, raises MessageConflict. A second run while one is active raises
        ConversationBusy.
        """
        _check_id(message_id, "message_id")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text must be a non-empty string")
        with self._tx() as db:
            self._conversation(db, conversation_id)
            existing = db.execute("SELECT * FROM pi_messages WHERE id=?", (message_id,)).fetchone()
            if existing is not None:
                if (existing["conversation_id"] != conversation_id or existing["role"] != "user"
                        or existing["body"] != text):
                    raise MessageConflict("message ID already used with different content")
                run = db.execute("SELECT * FROM pi_runs WHERE message_id=?", (message_id,)).fetchone()
                return {"message": self._message(existing), "run": self._run_dict(run), "created": False}
            if db.execute("SELECT 1 FROM pi_runs WHERE conversation_id=? AND status IN (?, ?)",
                          (conversation_id, *ACTIVE_RUN)).fetchone():
                raise ConversationBusy("conversation already has an active run")
            stamp, run_id = self._stamp(), _new_id("run")
            seq = db.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM pi_messages WHERE conversation_id=?",
                             (conversation_id,)).fetchone()[0]
            db.execute("INSERT INTO pi_messages VALUES (?, ?, ?, 'user', ?, ?, ?)",
                       (message_id, conversation_id, seq, text, stamp, run_id))
            db.execute("INSERT INTO pi_runs VALUES (?, ?, ?, 'running', NULL, ?, NULL, NULL)",
                       (run_id, conversation_id, message_id, stamp))
            db.execute("UPDATE pi_conversations SET updated_at=? WHERE id=?", (stamp, conversation_id))
            self._emit(db, conversation_id, "message",
                       {"id": message_id, "role": "user", "text": text, "captured_at": stamp},
                       run_id, stamp)
            self._emit(db, conversation_id, "run_started", {"run_id": run_id, "message_id": message_id},
                       run_id, stamp)
            message = self._message(db.execute("SELECT * FROM pi_messages WHERE id=?", (message_id,)).fetchone())
            run = self._run_dict(self._run(db, run_id))
        return {"message": message, "run": run, "created": True}

    def add_assistant_message(self, run_id, text, message_id=None):
        """Record the assistant reply; rejected once the run was cancelled or ended."""
        message_id = message_id or _new_id("msg")
        _check_id(message_id, "message_id")
        with self._tx() as db:
            run = self._run(db, run_id)
            if run["status"] != "running":
                raise RunNotActive(f"run is {run['status']}")
            stamp, conversation_id = self._stamp(), run["conversation_id"]
            seq = db.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM pi_messages WHERE conversation_id=?",
                             (conversation_id,)).fetchone()[0]
            db.execute("INSERT INTO pi_messages VALUES (?, ?, ?, 'assistant', ?, ?, ?)",
                       (message_id, conversation_id, seq, text, stamp, run_id))
            db.execute("UPDATE pi_conversations SET updated_at=? WHERE id=?", (stamp, conversation_id))
            self._emit(db, conversation_id, "message",
                       {"id": message_id, "role": "assistant", "text": text, "captured_at": stamp},
                       run_id, stamp)
        return message_id

    def request_cancel(self, run_id):
        """Persist the cancel request first; idempotent. Returns the run status."""
        with self._tx() as db:
            run = self._run(db, run_id)
            if run["status"] != "running":
                return run["status"]
            db.execute("UPDATE pi_runs SET status='cancelling' WHERE id=?", (run_id,))
            self._emit(db, run["conversation_id"], "cancel_requested", {"run_id": run_id}, run_id)
        return "cancelling"

    def run_status(self, run_id):
        with self._tx(write=False) as db:
            return self._run(db, run_id)["status"]

    def finish_run(self, run_id, status, stop_reason=None, usage=None):
        """End a run. ``usage=None`` is stored as unknown, never as zero.

        A run whose cancel was requested always ends as ``cancelled``. Finishing an
        already-final run is a no-op that returns its existing status.
        """
        if status not in ("completed", "failed", "cancelled"):
            raise ValueError("status must be completed, failed or cancelled")
        with self._tx() as db:
            run = self._run(db, run_id)
            if run["status"] in FINAL_RUN:
                return run["status"]
            if run["status"] == "cancelling":
                status = "cancelled"
            usage_record = {"complete": False} if usage is None else {"complete": True, **usage}
            stamp = self._stamp()
            db.execute("UPDATE pi_runs SET status=?, stop_reason=?, ended_at=?, usage=? WHERE id=?",
                       (status, stop_reason, stamp, _dump(usage_record), run_id))
            self._emit(db, run["conversation_id"], "run_finished",
                       {"run_id": run_id, "status": status, "stop_reason": stop_reason,
                        "usage": usage_record}, run_id, stamp)
        return status

    def recover_interrupted(self):
        """Call once at service start: unfinished runs become ``interrupted`` and are not replayed."""
        with self._tx() as db:
            runs = db.execute("SELECT * FROM pi_runs WHERE status IN (?, ?)", ACTIVE_RUN).fetchall()
            stamp = self._stamp()
            for run in runs:
                db.execute("UPDATE pi_runs SET status='interrupted', stop_reason='service_restart', "
                           "ended_at=?, usage=? WHERE id=?",
                           (stamp, _dump({"complete": False}), run["id"]))
                db.execute("UPDATE pi_tool_calls SET status='unknown' WHERE run_id=? AND status='pending'",
                           (run["id"],))
                self._emit(db, run["conversation_id"], "run_finished",
                           {"run_id": run["id"], "status": "interrupted", "stop_reason": "service_restart",
                            "usage": {"complete": False}}, run["id"], stamp)
        return [run["id"] for run in runs]

    # -- events -------------------------------------------------------------
    def append_event(self, conversation_id, kind, payload, run_id=None):
        with self._tx() as db:
            self._conversation(db, conversation_id)
            return self._emit(db, conversation_id, kind, payload, run_id)

    def events_after(self, conversation_id, after=0, limit=500):
        with self._tx(write=False) as db:
            self._conversation(db, conversation_id)
            rows = db.execute("SELECT * FROM pi_events WHERE conversation_id=? AND seq>? ORDER BY seq LIMIT ?",
                              (conversation_id, after, limit)).fetchall()
        return [{"seq": r["seq"], "run_id": r["run_id"], "type": r["type"],
                 "payload": json.loads(r["payload"]), "created_at": r["created_at"]} for r in rows]

    # -- Pi SDK entries -----------------------------------------------------
    def save_entries(self, conversation_id, entries):
        """Upsert SDK entries by ID; new IDs keep arrival order, updates keep their position."""
        with self._tx() as db:
            self._conversation(db, conversation_id)
            for entry in entries:
                entry_id = entry.get("id")
                _check_id(entry_id, "entry id")
                body = _dump(entry)
                updated = db.execute("UPDATE pi_entries SET body=? WHERE conversation_id=? AND id=?",
                                     (body, conversation_id, entry_id)).rowcount
                if not updated:
                    seq = db.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM pi_entries WHERE conversation_id=?",
                                     (conversation_id,)).fetchone()[0]
                    db.execute("INSERT INTO pi_entries VALUES (?, ?, ?, ?)",
                               (conversation_id, seq, entry_id, body))

    def load_entries(self, conversation_id):
        with self._tx(write=False) as db:
            self._conversation(db, conversation_id)
            return [json.loads(r[0]) for r in db.execute(
                "SELECT body FROM pi_entries WHERE conversation_id=? ORDER BY seq", (conversation_id,))]

    # -- tools and evidence -------------------------------------------------
    def begin_tool_call(self, run_id, call_id, name, args):
        """Register a tool call before executing it.

        Returns None when the caller should execute the call now. Returns the stored
        result when the same ID and arguments already completed. Raises
        ToolCallConflict for the same ID with different arguments, ToolOutcomeUnknown
        when the earlier attempt never committed, and RunNotActive after cancellation.
        """
        _check_id(call_id, "call_id")
        args_json = _dump(args)
        with self._tx() as db:
            run = self._run(db, run_id)
            existing = db.execute("SELECT * FROM pi_tool_calls WHERE id=?", (call_id,)).fetchone()
            if existing is not None:
                if (existing["conversation_id"] != run["conversation_id"] or existing["name"] != name
                        or existing["args"] != args_json):
                    raise ToolCallConflict("tool call ID already used with different arguments")
                if existing["status"] in ("complete", "failed"):
                    return json.loads(existing["result"])
                if existing["status"] == "discarded":
                    raise RunNotActive("tool call belongs to a run that is no longer active")
                raise ToolOutcomeUnknown("earlier attempt has no committed result; use a new call ID")
            if run["status"] != "running":
                raise RunNotActive(f"run is {run['status']}")
            stamp = self._stamp()
            db.execute("INSERT INTO pi_tool_calls VALUES (?, ?, ?, ?, ?, 'pending', NULL, ?, NULL)",
                       (call_id, run["conversation_id"], run_id, name, args_json, stamp))
            self._emit(db, run["conversation_id"], "tool_started",
                       {"call_id": call_id, "name": name, "args": args}, run_id, stamp)
        return None

    def complete_tool_call(self, call_id, result, evidence=()):
        """Commit the result and its evidence in one transaction.

        Each evidence item is ``{"facts": dict, "raw": any, "fetched_at": iso str}``;
        ``raw`` stays private. Returns the stored result (with ``evidence_ids``), or
        None if the run is no longer running: the result is archived but must not be
        handed back to the model.
        """
        return self._resolve_tool_call(call_id, "complete", dict(result), list(evidence))

    def fail_tool_call(self, call_id, code, message):
        return self._resolve_tool_call(call_id, "failed",
                                       {"status": "error", "error": {"code": code, "message": message}}, [])

    def _resolve_tool_call(self, call_id, status, result, evidence):
        with self._tx() as db:
            call = db.execute("SELECT * FROM pi_tool_calls WHERE id=?", (call_id,)).fetchone()
            if call is None:
                raise NotFound("tool call not found")
            if call["status"] != "pending":
                raise ToolCallConflict(f"tool call already {call['status']}")
            run = self._run(db, call["run_id"])
            applied = run["status"] == "running"
            stamp, ids = self._stamp(), []
            for item in evidence:
                evidence_id = item.get("id") or _new_id("ev")
                _check_id(evidence_id, "evidence id")
                db.execute("INSERT INTO pi_evidence VALUES (?, ?, ?, ?, ?, ?)",
                           (evidence_id, call["conversation_id"], call_id, item.get("fetched_at") or stamp,
                            _dump(item["facts"]), _dump(item.get("raw"))))
                ids.append(evidence_id)
            stored = {**result, "evidence_ids": ids}
            db.execute("UPDATE pi_tool_calls SET status=?, result=?, completed_at=? WHERE id=?",
                       (status if applied else "discarded", _dump(stored), stamp, call_id))
            if applied:
                self._emit(db, call["conversation_id"], "tool_result",
                           {"call_id": call_id, "name": call["name"], "status": status, "evidence_ids": ids},
                           call["run_id"], stamp)
        return stored if applied else None

    def get_evidence(self, conversation_id, evidence_id, include_raw=False):
        """Evidence is only visible inside its own conversation."""
        with self._tx(write=False) as db:
            row = db.execute("SELECT * FROM pi_evidence WHERE id=? AND conversation_id=?",
                             (evidence_id, conversation_id)).fetchone()
        if row is None:
            raise NotFound("evidence not found")
        item = {"id": row["id"], "tool_call_id": row["tool_call_id"], "fetched_at": row["fetched_at"],
                "facts": json.loads(row["facts"])}
        if include_raw:
            item["raw"] = json.loads(row["raw"]) if row["raw"] is not None else None
        return item
