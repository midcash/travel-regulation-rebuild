"""Atomic local checkpoints, separate from supplier evidence and telemetry."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3


class StorageError(RuntimeError):
    pass


class SessionStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with closing(self.connect()) as db:
                version = db.execute("PRAGMA user_version").fetchone()[0]
                tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                if version not in (0, 1) or (version == 0 and tables):
                    raise StorageError("Unsupported session database version")
                with db:
                    db.execute("CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
                    db.execute("CREATE TABLE IF NOT EXISTS active(singleton INTEGER PRIMARY KEY CHECK(singleton=1), id TEXT NOT NULL)")
                    db.execute("PRAGMA user_version=1")
        except sqlite3.Error as error:
            raise StorageError("Cannot open session database") from error

    def connect(self):
        return sqlite3.connect(self.path, timeout=.5)

    def load(self):
        try:
            with closing(self.connect()) as db:
                active = db.execute("SELECT id FROM active WHERE singleton=1").fetchone()
                if active is None:
                    if db.execute("SELECT count(*) FROM sessions").fetchone()[0]:
                        raise StorageError("Missing active session")
                    return None
                row = db.execute("SELECT payload FROM sessions WHERE id=?", active).fetchone()
                if row is None:
                    raise StorageError("Missing session checkpoint")
                state = json.loads(row[0])
                if state['version'] != 1 or state['public']['session_id'] != active[0]:
                    raise StorageError("Invalid session checkpoint")
                return state
        except (sqlite3.Error, ValueError, KeyError, TypeError) as error:
            raise StorageError("Cannot restore session checkpoint") from error

    def save(self, state):
        payload = json.dumps(state, ensure_ascii=False, allow_nan=False)
        try:
            with closing(self.connect()) as db, db:
                identifier = state['public']['session_id']
                db.execute("INSERT INTO sessions VALUES (?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                           (identifier, payload))
                db.execute("INSERT INTO active VALUES(1,?) ON CONFLICT(singleton) DO UPDATE SET id=excluded.id", (identifier,))
        except sqlite3.Error as error:
            raise StorageError("会话无法保存，请检查本地存储后重试。") from error
