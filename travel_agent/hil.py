"""Durable local human-in-the-loop entry points over the shared agent kernel.

SQLite stores private snapshots. An OS file lock prevents simultaneous local
executors, including across processes; an owner token fences database writes.
External calls never run inside a database transaction.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import copy
import json
import os
import sqlite3
import time
import uuid

from .agent import BudgetExceeded, TripExecution, final_fields
from .domain import CHINA, fingerprint, handoff
from .human import HumanInputError, candidate_question, check_decisions, prepare_question, validate_messages, validate_reply


TERMINAL = {"completed", "failed", "cancelled", "expired"}


class HilConflict(Exception):
    pass


class HilStorageError(Exception):
    pass


def now():
    return datetime.now(timezone.utc)


def _setup(store):
    with store.connect() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS hil_executions (
            run_id TEXT PRIMARY KEY, owner TEXT, version INTEGER NOT NULL,
            payload TEXT NOT NULL)""")


def _read(store, run_id):
    with store.connect() as db:
        row = db.execute("SELECT payload FROM hil_executions WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        raise KeyError("HIL run not found")
    state = json.loads(row[0])
    if state.get("version") != 1:
        raise HilConflict("Incompatible HIL snapshot")
    if state.get("result") is None and state["execution"].get("schema_version") != 2:
        raise HilConflict("Incompatible execution snapshot")
    return state


@contextmanager
def _ownership(store, run_id):
    if not isinstance(run_id, str) or len(run_id) != 32 or any(c not in "0123456789abcdef" for c in run_id):
        raise HumanInputError("Invalid run ID")
    database = store.path.resolve()
    directory = database.parent / (database.name + ".hil-locks")
    directory.mkdir(exist_ok=True)
    with (directory / run_id).open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise HilConflict("Run is already executing") from None
        token = uuid.uuid4().hex
        try:
            with store.connect() as db:
                db.execute("UPDATE hil_executions SET owner=? WHERE run_id=?", (token, run_id))
            yield token
        finally:
            try:
                with store.connect() as db:
                    db.execute("UPDATE hil_executions SET owner=NULL WHERE run_id=? AND owner=?", (run_id, token))
            finally:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle, fcntl.LOCK_UN)


def _expired(state):
    return (state["execution_status"] == "waiting_for_human"
            and now() >= datetime.fromisoformat(state["human_request"]["expires_at"]))


def _envelope(state):
    status = "expired" if _expired(state) else state["execution_status"]
    return {"run_id": state["run_id"], "trace_id": state["run_id"],
            "execution_status": status, "state_version": state["state_version"],
            "human_request": copy.deepcopy(state["human_request"]) if status == "waiting_for_human" else None,
            "result": copy.deepcopy(state.get("result"))}


def _save(store, state, owner):
    """Snapshot and public projection commit together, guarded by owner/version."""
    previous = state["state_version"]
    candidate = copy.deepcopy(state)
    candidate["state_version"] += 1
    run = copy.deepcopy(candidate.get("result") or candidate["execution"]["run"])
    run["execution_status"] = candidate["execution_status"]
    if candidate.get("result") is None:
        run["status"] = "running"
        run["tool_calls"] = candidate["execution"]["tool_state"]["calls"]
    try:
        with store.connect() as db:
            cursor = db.execute("UPDATE hil_executions SET version=?,payload=? WHERE run_id=? AND owner=? AND version=?",
                (candidate["state_version"], json.dumps(candidate, ensure_ascii=False), candidate["run_id"], owner, previous))
            if cursor.rowcount != 1:
                raise HilConflict("Execution ownership or version changed")
            db.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?)",
                (run["trace_id"], run["started_at"], json.dumps(run, ensure_ascii=False)))
    except sqlite3.Error:
        raise HilStorageError("Unable to commit execution") from None
    state.clear()
    state.update(candidate)


def get_trip(run_id, store):
    """Read-only, including the effective expiry projection; never drive work."""
    return _envelope(_read(store, run_id))


def _wait(state, execution, args, origin):
    if state["human_requests"] >= state["max_human_requests"]:
        raise BudgetExceeded("human_requests")
    question = prepare_question(args, execution.toolbox.seen)
    moment = now()
    state["human_request"] = {**question, "interaction_id": uuid.uuid4().hex,
        "origin": origin, "created_at": moment.isoformat(),
        "expires_at": (moment + timedelta(minutes=state["human_ttl_minutes"])).isoformat()}
    state["human_requests"] += 1
    state["execution_status"] = "waiting_for_human"
    execution.stage = "await_human"


def _end_saved(state, reason, status):
    execution = state["execution"]
    execution["verdict"] = handoff()
    execution["run"]["termination"] = reason
    execution["stage"] = "finalize"
    state["terminal_status"] = status
    state["execution_status"] = "running"
    state["human_request"] = None


def _finish_saved(store, state, owner):
    execution = state["execution"]
    run = execution["run"]
    run.update(final_fields(execution["verdict"], execution["request"], execution["tool_mode"],
        execution["tool_results"], execution["tool_state"], (time.time() - execution["wall_started"]) * 1000))
    state["execution_status"] = state.get("terminal_status", "completed" if run["termination"] == "completed" else "failed")
    run["active_duration_ms"] = round(execution["active_s"] * 1000, 3)
    run["waiting_duration_ms"] = round(state["waiting_s"] * 1000, 3)
    run["human_requests"] = state["human_requests"]
    run["execution_status"] = state["execution_status"]
    state["result"] = copy.deepcopy(run)
    execution["stage"] = "done"
    _save(store, state, owner)
    return _envelope(state)


def start_trip(scenario, provider, store, limits=None, *, tool_mode="demo", policy_path=None,
               transport_http=None, otel=None, max_human_requests=3, human_ttl_minutes=1440,
               confirm_request=True):
    if type(max_human_requests) is not int or not 1 <= max_human_requests <= 20:
        raise HumanInputError("Invalid human request limit")
    if type(human_ttl_minutes) not in {int, float} or not 0 < human_ttl_minutes <= 43200:
        raise HumanInputError("Invalid human TTL")
    _setup(store)
    execution = TripExecution(scenario, provider, store, limits, otel,
        tool_mode=tool_mode, policy_path=policy_path, transport_http=transport_http)
    execution.enable_human_input()
    state = {"version": 1, "run_id": execution.run["trace_id"], "state_version": 0,
        "execution_status": "running", "execution": execution.snapshot(),
        "human_request": None, "human_requests": 0, "decisions": [], "responses": {},
        "max_human_requests": max_human_requests, "human_ttl_minutes": human_ttl_minutes,
        "waiting_s": 0, "inflight": None, "result": None}
    if confirm_request:
        request = execution.request
        question = f'{request["origin"]} → {request["destination"]}，{request["meeting_location"]}，会议时间 {request["meeting_at"]}。请确认；可补充偏好，更改核心需求请新建任务。'
        _wait(state, execution, {"kind": "confirm", "question": question, "options": []}, "guardrail")
    state["execution"] = execution.snapshot()
    with store.connect() as db:
        db.execute("INSERT INTO hil_executions VALUES (?,NULL,0,?)", (state["run_id"], json.dumps(state, ensure_ascii=False)))
    with _ownership(store, state["run_id"]) as owner:
        _save(store, state, owner)
    return resume_trip(state["run_id"], store, lambda _: provider, otel=otel, transport_http=transport_http)


def _restore(state, store, provider_factory, **runtime):
    config = {key: state["execution"]["run"][key] for key in ("provider", "model", "pricing_usd_per_million")}
    config.update(state["execution"].get("provider_config", {}))
    return TripExecution.restore(state["execution"], provider_factory(config), store, **runtime)


def _drive(state, store, owner, provider_factory, **runtime):
    if state["execution_status"] == "waiting_for_human" and not _expired(state):
        return _envelope(state)
    if state["execution"]["stage"] == "finalize":
        return _finish_saved(store, state, owner)
    if state["inflight"]:
        run = state["execution"]["run"]
        run["active_duration_complete"] = False
        if state["inflight"]["kind"] == "model":
            run["usage_complete"], run["cost_usd"] = False, None
        elif state["execution"]["tool_mode"] == "real":
            run["http_requests_complete"] = False
        _end_saved(state, "execution_interrupted", "failed")
    if _expired(state):
        state["waiting_s"] += (now() - datetime.fromisoformat(state["human_request"]["created_at"])).total_seconds()
        _end_saved(state, "human_expired", "expired")
    if state["execution"]["stage"] == "finalize":
        _save(store, state, owner)
        return _finish_saved(store, state, owner)
    if state["execution_status"] in TERMINAL or state["execution_status"] == "waiting_for_human":
        return _envelope(state)
    execution = _restore(state, store, provider_factory, **runtime)
    execution.human_decisions = state["decisions"]

    def before_external(kind):
        snap = execution.snapshot()
        if kind == "tool":
            # Reservation is persisted, but the live toolbox increments itself.
            snap["tool_state"]["calls"] += 1
        state["execution"] = snap
        state["inflight"] = {"kind": kind, "at": now().isoformat(),
            "pending_call": copy.deepcopy(execution.pending_call)}
        _save(store, state, owner)

    execution.before_external = before_external
    try:
        with execution.tracer.span("invoke_agent", segment_id=uuid.uuid4().hex, human_loop=True):
            while execution.stage not in {"await_human", "finalize"}:
                if execution.stage == "model_turn":
                    validate_messages(execution.messages)
                    execution.advance()
                else:
                    execution.check_budget()
                    if execution.pending_call["name"] == "request_human_input":
                        _wait(state, execution, execution.pending_call["args"], "model")
                    else:
                        try:
                            check_decisions(execution.pending_call, state["decisions"], execution.toolbox.seen)
                        except ValueError:
                            execution.run["guardrail_blocks"] += 1
                            raise
                        execution.advance()
                        if execution.stage == "model_turn":
                            question = candidate_question(execution, state["decisions"])
                            if question is not None:
                                _wait(state, execution, question, "guardrail")
                state["inflight"] = None
                state["execution"] = execution.snapshot()
                _save(store, state, owner)
    except (HilStorageError, HilConflict, sqlite3.Error):
        raise
    except Exception as exc:
        execution.fail(exc)
        state["terminal_status"] = "failed"
        state["execution_status"] = "running"
        state["inflight"] = None
        state["execution"] = execution.snapshot()
        _save(store, state, owner)
    if execution.stage == "finalize":
        state["execution"] = execution.snapshot()
        return _finish_saved(store, state, owner)
    # Include span finalization and orchestration in active time before waiting.
    state["execution"] = execution.snapshot()
    _save(store, state, owner)
    return _envelope(state)


def resume_trip(run_id, store, provider_factory, **runtime):
    with _ownership(store, run_id) as owner:
        state = _read(store, run_id)
        if state["execution_status"] in TERMINAL and state.get("result") is not None:
            return _envelope(state)
        return _drive(state, store, owner, provider_factory, **runtime)


def respond_trip(run_id, store, provider_factory, *, interaction_id, state_version, response_id, reply, **runtime):
    if not isinstance(response_id, str) or not 1 <= len(response_id) <= 120:
        raise HumanInputError("Invalid response ID")
    digest = fingerprint({"interaction_id": interaction_id, "reply": reply})
    with _ownership(store, run_id) as owner:
        state = _read(store, run_id)
        previous = state["responses"].get(response_id)
        if previous is not None:
            if previous != digest:
                raise HilConflict("Response ID already used with different content")
            return _envelope(state)
        if _expired(state):
            _drive(state, store, owner, provider_factory, **runtime)
            raise HilConflict("Human request expired")
        question = state["human_request"]
        if (state["execution_status"] != "waiting_for_human" or question["interaction_id"] != interaction_id
                or state["state_version"] != state_version):
            raise HilConflict("Interaction or version changed")
        reply = validate_reply(question, reply)
        request = state["execution"]["request"]
        required_date = request.get("required_arrival_date", request.get("earliest_departure", "")[:10])
        if (now() >= datetime.fromisoformat(request["meeting_at"])
                or (required_date and required_date < str(now().astimezone(CHINA).date()))):
            _end_saved(state, "request_expired", "expired")
            _save(store, state, owner)
            return _finish_saved(store, state, owner)
        execution = _restore(state, store, provider_factory, **runtime)
        decision = {"interaction_id": interaction_id, "reply": reply, "accepted_at": now().isoformat()}
        if question["kind"] == "select" and not reply.get("rejected"):
            decision["selected"] = next(x for x in question["options"] if x["option_id"] == reply["option_id"])
        if question["kind"] == "select":
            decision["candidate_call_id"] = execution.toolbox.seen[question["options"][0]["evidence_id"]].get("call_id")
            if reply.get("rejected"):
                decision["rejected_options"] = question["options"]
        content = json.dumps({"human_response": reply}, ensure_ascii=False)
        if question["origin"] == "model":
            execution.messages.append({"role": "tool", "tool_name": "request_human_input",
                "tool_call_id": execution.pending_call["call"]["id"], "content": content})
            execution.pending_call = None
        else:
            execution.messages.append({"role": "user", "content": "Application human context: " + content})
        validate_messages(execution.messages)
        execution.stage = "dispatch" if execution.pending_call else "model_turn"
        state["responses"][response_id] = digest
        state["decisions"].append(decision)
        state["waiting_s"] += (now() - datetime.fromisoformat(question["created_at"])).total_seconds()
        state["execution_status"], state["human_request"] = "running", None
        state["execution"] = execution.snapshot()
        _save(store, state, owner)
        return _drive(state, store, owner, provider_factory, **runtime)


def cancel_trip(run_id, store, provider_factory, *, state_version, **runtime):
    with _ownership(store, run_id) as owner:
        state = _read(store, run_id)
        if state["execution_status"] in TERMINAL:
            return _envelope(state)
        if _expired(state):
            return _drive(state, store, owner, provider_factory, **runtime)
        if state["execution_status"] != "waiting_for_human" or state["state_version"] != state_version:
            raise HilConflict("Only the current waiting interaction can be cancelled")
        state["waiting_s"] += (now() - datetime.fromisoformat(state["human_request"]["created_at"])).total_seconds()
        _end_saved(state, "human_cancelled", "cancelled")
        _save(store, state, owner)
        return _finish_saved(store, state, owner)
