"""Retained local trace behavior; no access to the user's trace database."""
from pathlib import Path
from contextlib import closing
import sqlite3
import tempfile
import unittest

from travel_agent.telemetry import Store, Tracer


class LocalTrace(unittest.TestCase):
    def test_nested_spans_and_errors_persist_without_exception_message(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "trace.sqlite3"
            store = Store(path)
            tracer = Tracer(store)
            with tracer.span("run"):
                with self.assertRaises(ValueError):
                    with tracer.span("tool"):
                        raise ValueError("private-secret")
            store.save_run({"trace_id": tracer.trace_id, "started_at": "2099-09-18T00:00:00+08:00"})
            loaded = Store(path).get_run(tracer.trace_id)
            self.assertEqual(len(loaded["spans"]), 2)
            spans = {s["name"]: s for s in loaded["spans"]}
            root, tool = spans["run"], spans["tool"]
            self.assertEqual(tool["parent_span_id"], root["span_id"])
            self.assertEqual(tool["status"], "error")
            self.assertEqual(tool["attributes"]["error.type"], "ValueError")
            self.assertNotIn("private-secret", str(loaded))

    def test_initialization_preserves_existing_tables_and_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "trace.sqlite3"
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("CREATE TABLE hil_executions (id TEXT PRIMARY KEY, value TEXT)")
                db.execute("INSERT INTO hil_executions VALUES ('old', 'keep')")
                db.execute("CREATE TABLE evaluations (id TEXT PRIMARY KEY, value TEXT)")
                db.execute("INSERT INTO evaluations VALUES ('old-eval', 'keep')")
            Store(path)
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute("SELECT * FROM hil_executions").fetchall(), [("old", "keep")])
                self.assertEqual(db.execute("SELECT * FROM evaluations").fetchall(), [("old-eval", "keep")])
