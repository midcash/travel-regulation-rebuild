from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import uuid
import threading
from functools import wraps

CHINA = timezone(timedelta(hours=8))
TERMINAL = {"completed", "cancelled", "failed", "expired"}


class HilError(Exception):
    pass


class HilStore:
    def __init__(self, path: str | Path):
        self._local = threading.local()
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY, status TEXT NOT NULL,
                    state_json TEXT NOT NULL, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS human_interactions (
                    interaction_id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
                    kind TEXT NOT NULL, status TEXT NOT NULL,
                    state_version INTEGER NOT NULL, payload_json TEXT NOT NULL,
                    response_json TEXT, response_id TEXT UNIQUE,
                    created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
                    consumed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL, event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL, created_at TEXT NOT NULL
                );
            """)

    @contextmanager
    def connect(self):
        existing = getattr(self._local, "db", None)
        if existing is not None:
            yield existing
            return
        db = sqlite3.connect(str(self.path), timeout=10)
        db.row_factory = sqlite3.Row
        self._local.db = db
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                yield db
        finally:
            self._local.db = None
            db.close()

    def list_runs(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT run_id, status, created_at FROM runs ORDER BY created_at DESC LIMIT 100")]

    def create_run(self, run_id, state):
        now = utcnow()
        with self.connect() as db:
            db.execute("INSERT INTO runs VALUES (?, ?, ?, ?, ?)",
                       (run_id, state["status"], encode(state), now, now))
            event(db, run_id, "run_created", {"status": state["status"]})

    def get_run(self, run_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise HilError(f"run not found: {run_id}")
        state = decode(row["state_json"])
        state.update({"run_id": row["run_id"], "status": row["status"]})
        return state

    def save_run(self, run_id, state, event_type, payload):
        now = utcnow()
        with self.connect() as db:
            updated = db.execute(
                "UPDATE runs SET status=?, state_json=?, updated_at=? WHERE run_id=?",
                (state["status"], encode(state), now, run_id))
            if updated.rowcount != 1:
                raise HilError(f"run not found: {run_id}")
            event(db, run_id, event_type, payload)

    def create_interaction(self, run_id, kind, state_version, payload, ttl_minutes=60):
        interaction = {
            "interaction_id": uuid.uuid4().hex, "run_id": run_id,
            "kind": kind, "status": "pending", "state_version": state_version,
            "payload": payload, "created_at": utcnow(),
            "expires_at": (datetime.now(CHINA) + timedelta(minutes=ttl_minutes)).isoformat()
        }
        with self.connect() as db:
            db.execute(
                """INSERT INTO human_interactions
                   (interaction_id, run_id, kind, status, state_version,
                    payload_json, response_json, response_id, created_at,
                    expires_at, consumed_at)
                   VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, NULL)""",
                (interaction["interaction_id"], run_id, kind, "pending",
                 state_version, encode(payload), interaction["created_at"],
                 interaction["expires_at"]))
            event(db, run_id, "human_requested",
                  {"interaction_id": interaction["interaction_id"], "kind": kind})
        return interaction

    def get_interaction(self, interaction_id):
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM human_interactions WHERE interaction_id=?",
                (interaction_id,)).fetchone()
        if row is None:
            raise HilError(f"interaction not found: {interaction_id}")
        return interaction_from_row(row)

    def consume_interaction(self, interaction_id, response_id, expected_version, response):
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM human_interactions WHERE interaction_id=?",
                (interaction_id,)).fetchone()
            if row is None:
                raise HilError(f"interaction not found: {interaction_id}")
            current = interaction_from_row(row)
            if current["status"] == "consumed":
                if current["response_id"] == response_id:
                    return current
                raise HilError("interaction already consumed")
            if current["status"] != "pending":
                raise HilError(f"interaction is {current['status']}")
            if current["state_version"] != expected_version:
                raise HilError("stale state version")
            # Expiry is handled by TravelHilLoop before consuming, in this transaction.
            db.execute(
                """UPDATE human_interactions
                   SET status='consumed', response_json=?, response_id=?, consumed_at=?
                   WHERE interaction_id=? AND status='pending'""",
                (encode(response), response_id, utcnow(), interaction_id))
            event(db, current["run_id"], "human_responded",
                  {"interaction_id": interaction_id, "response_id": response_id})
            current.update({"status": "consumed", "response": response,
                            "response_id": response_id})
            return current

    def list_events(self, run_id):
        with self.connect() as db:
            rows = db.execute(
                "SELECT event_type, payload_json, created_at FROM events "
                "WHERE run_id=? ORDER BY event_id", (run_id,)).fetchall()
        return [{"type": row["event_type"], "payload": decode(row["payload_json"]),
                 "created_at": row["created_at"]} for row in rows]


def atomic(method):
    """Commit the reply, checkpoint and next request as one offline transition."""
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self.store.connect():
            return method(self, *args, **kwargs)
    return wrapped


class TravelHilLoop:
    def __init__(self, store):
        self.store = store

    @atomic
    def start(self, request=None):
        request = request or default_request()
        validate_request(request)
        run_id = uuid.uuid4().hex
        state = {
            "status": "running", "stage": "search", "request": request,
            "request_version": 1, "state_version": 0, "candidates": [],
            "selected": {}, "details": [], "plan": None, "tool_calls": 0,
            "last_interaction_id": None
        }
        self.store.create_run(run_id, state)
        return self._advance(run_id, state)

    @atomic
    def respond(self, interaction_id, action, payload=None,
                response_id=None, expected_state_version=None):
        interaction = self.store.get_interaction(interaction_id)
        payload = payload or {}
        response_id = response_id or uuid.uuid4().hex
        response = {"action": action, "payload": payload}
        if interaction["status"] == "consumed":
            if interaction["response_id"] == response_id:
                if interaction.get("response") != response:
                    raise HilError("response ID reused with different content")
                return self.resume(interaction["run_id"])
            raise HilError("interaction already consumed")
        expected_state_version = (
            interaction["state_version"]
            if expected_state_version is None else expected_state_version)
        response = {"action": action, "payload": payload}
        before = self.store.get_run(interaction["run_id"])
        if before["status"] in TERMINAL:
            raise HilError(f"run is {before['status']}")
        if before["last_interaction_id"] != interaction_id:
            raise HilError("interaction is not the active request")
        if self._expire(before, interaction):
            return public_state(interaction["run_id"], before)
        self._validate_response(before, interaction, response)
        consumed = self.store.consume_interaction(
            interaction_id, response_id, expected_state_version, response)
        state = self.store.get_run(consumed["run_id"])
        if state["status"] in TERMINAL:
            raise HilError(f"run is {state['status']}")
        self._apply_response(state, interaction, response)
        state["state_version"] += 1
        return self._advance(consumed["run_id"], state)

    def _validate_response(self, state, interaction, response):
        action, payload = response["action"], response["payload"]
        if interaction["kind"] == "select_candidate":
            if action == "cancel":
                return
            if action != "select":
                raise HilError("candidate request only accepts select or cancel")
            transport_id, stay_id = payload.get("transport_id"), payload.get("stay_id")
            valid = {item["id"] for item in state["candidates"]}
            if transport_id not in valid or stay_id not in valid:
                raise HilError("selection must reference current candidates")
            if not transport_id.startswith("transport_") or not stay_id.startswith("stay_"):
                raise HilError("select one transport and one stay")
            return
        if interaction["kind"] == "approve_plan":
            if action == "cancel":
                return
            if action == "approve":
                if state["plan"]["status"] != "planned":
                    raise HilError("an infeasible plan cannot be approved")
                return
            if action == "revise":
                patch = payload.get("request_patch", {})
                if set(patch) - {"budget_cny"}:
                    raise HilError("only budget_cny can be revised in this prototype")
                revised = dict(state["request"])
                revised.update(patch)
                validate_request(revised)
                return
            raise HilError("approval request only accepts approve, revise or cancel")
        raise HilError(f"unknown interaction kind: {interaction['kind']}")

    def _expire(self, state, interaction):
        if interaction["status"] == "pending" and datetime.now(CHINA) >= datetime.fromisoformat(interaction["expires_at"]):
            with self.store.connect() as db:
                db.execute("UPDATE human_interactions SET status='expired' WHERE interaction_id=?",
                           (interaction["interaction_id"],))
            state["status"], state["stage"] = "expired", "expired"
            self.store.save_run(interaction["run_id"], state, "human_expired",
                                {"interaction_id": interaction["interaction_id"]})
            return True
        return False

    @atomic
    def resume(self, run_id):
        state = self.store.get_run(run_id)
        if state["status"] in TERMINAL:
            return public_state(run_id, state)
        if state["status"] == "running":
            return self._advance(run_id, state)
        interaction = self.store.get_interaction(state["last_interaction_id"])
        if self._expire(state, interaction):
            return public_state(run_id, state)
        # Recover checkpoints left by the older non-atomic prototype.
        if interaction["status"] == "consumed":
            self._validate_response(state, interaction, interaction["response"])
            self._apply_response(state, interaction, interaction["response"])
            state["state_version"] += 1
            return self._advance(run_id, state)
        result = public_state(run_id, state)
        result["human_request"] = interaction
        return result

    def _advance(self, run_id, state):
        if state["stage"] == "search":
            state["candidates"] = search_candidates(state["request"])
            state["tool_calls"] += 1
            state["stage"] = "await_selection"
            return self._wait(run_id, state, "select_candidate", selection_payload(state))
        if state["stage"] == "details":
            state["details"] = get_details(state["selected"])
            state["tool_calls"] += 2
            state["plan"] = build_plan(state["request"], state["details"])
            state["stage"] = "await_approval"
            return self._wait(run_id, state, "approve_plan", approval_payload(state))
        if state["stage"] == "completed":
            self.store.save_run(run_id, state, "run_completed", {"plan": state["plan"]})
            return public_state(run_id, state)
        if state["stage"] == "cancelled":
            self.store.save_run(run_id, state, "run_cancelled", {})
            return public_state(run_id, state)
        raise HilError(f"unsupported stage: {state['stage']}")

    def _wait(self, run_id, state, kind, payload):
        state["status"] = "waiting_for_human"
        state["state_version"] += 1
        interaction = self.store.create_interaction(
            run_id, kind, state["state_version"], payload)
        state["last_interaction_id"] = interaction["interaction_id"]
        self.store.save_run(
            run_id, state, "run_waiting_for_human",
            {"interaction_id": interaction["interaction_id"], "kind": kind})
        result = public_state(run_id, state)
        result["human_request"] = interaction
        return result

    def _apply_response(self, state, interaction, response):
        action, payload = response["action"], response["payload"]
        if interaction["kind"] == "select_candidate":
            if action == "cancel":
                state["stage"], state["status"] = "cancelled", "cancelled"
                return
            state["selected"] = {
                "transport_id": payload["transport_id"],
                "stay_id": payload["stay_id"]}
            state["stage"], state["status"] = "details", "running"
            return
        if interaction["kind"] == "approve_plan":
            if action == "cancel":
                state["stage"], state["status"] = "cancelled", "cancelled"
                return
            if action == "approve":
                state["stage"], state["status"] = "completed", "completed"
                return
            state["request"].update(payload.get("request_patch", {}))
            state["request_version"] += 1
            state["candidates"], state["selected"] = [], {}
            state["details"], state["plan"] = [], None
            state["stage"], state["status"] = "search", "running"
            return
        raise HilError(f"unknown interaction kind: {interaction['kind']}")


def search_candidates(request):
    return [
        {"id": "transport_001", "kind": "transport", "label": "杭州东站 → 上海虹桥站",
         "start_at": "2026-09-11T19:00+08:00", "end_at": "2026-09-11T20:30+08:00",
         "price_cny": 73},
        {"id": "transport_002", "kind": "transport", "label": "杭州东站 → 上海虹桥站",
         "start_at": "2026-09-11T20:00+08:00", "end_at": "2026-09-11T21:40+08:00",
         "price_cny": 110},
        {"id": "stay_001", "kind": "stay", "label": "桔子水晶酒店",
         "price_cny": 400, "arrival_at": "2026-09-12T07:50+08:00"},
        {"id": "stay_002", "kind": "stay", "label": "远端商务酒店",
         "price_cny": 320, "arrival_at": "2026-09-12T08:10+08:00"},
    ]


def get_details(selected):
    details = {item["id"]: item for item in search_candidates(default_request())}
    transport, stay = details[selected["transport_id"]], details[selected["stay_id"]]
    return [
        transport,
        {"id": "route_001", "kind": "route", "label": "上海虹桥站 → 酒店", "price_cny": 120},
        stay,
        {"id": "route_002", "kind": "route", "label": "酒店 → 东方明珠塔",
         "price_cny": 35, "end_at": stay["arrival_at"]},
    ]


def build_plan(request, details):
    total = sum(item["price_cny"] for item in details)
    arrival_at, deadline = details[-1]["end_at"], "2026-09-12T07:50+08:00"
    feasible = total <= request["budget_cny"] and arrival_at <= deadline
    return {
        "status": "planned" if feasible else "needs_review",
        "reason": "feasible" if feasible else "policy_or_time_constraint",
        "total_cny": total, "arrival_at": arrival_at, "deadline": deadline,
        "evidence_refs": [item["id"] for item in details], "details": details}


def selection_payload(state):
    return {
        "question": "请选择一项交通和一项住宿，Agent 将继续查询详情并生成方案。",
        "options": state["candidates"], "required": ["transport_id", "stay_id"],
        "request_version": state["request_version"]}


def approval_payload(state):
    allowed = (["approve", "revise", "cancel"]
               if state["plan"]["status"] == "planned" else ["revise", "cancel"])
    return {"question": "请确认当前差旅方案。", "plan": state["plan"],
            "allowed_actions": allowed, "request_version": state["request_version"]}


def default_request():
    return {
        "origin": "杭州", "destination": "上海", "meeting_location": "东方明珠塔",
        "meeting_at": "2026-09-12T08:00+08:00", "budget_cny": 1000}


def validate_request(request):
    required = {"origin", "destination", "meeting_location", "meeting_at", "budget_cny"}
    if (set(request) != required or type(request["budget_cny"]) is not int
            or request["budget_cny"] < 0):
        raise HilError("request must contain valid travel fields and budget_cny")


def public_state(run_id, state):
    return {
        "run_id": run_id, "status": state["status"], "stage": state["stage"],
        "state_version": state["state_version"], "request_version": state["request_version"],
        "selected": state["selected"], "plan": state["plan"],
        "tool_calls": state["tool_calls"], "interaction_id": state["last_interaction_id"]}


def interaction_from_row(row):
    result = {
        "interaction_id": row["interaction_id"], "run_id": row["run_id"],
        "kind": row["kind"], "status": row["status"],
        "state_version": row["state_version"], "payload": decode(row["payload_json"]),
        "created_at": row["created_at"], "expires_at": row["expires_at"],
        "response_id": row["response_id"]}
    if row["response_json"]:
        result["response"] = decode(row["response_json"])
    return result


def event(db, run_id, event_type, payload):
    db.execute("INSERT INTO events(run_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
               (run_id, event_type, encode(payload), utcnow()))


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def decode(value):
    return json.loads(value)


def utcnow():
    return datetime.now(timezone.utc).isoformat()
