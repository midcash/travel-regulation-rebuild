from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import copy
import time

from . import __version__
from .domain import PACKAGE, fingerprint, handoff, validate_verdict, normalize_request, format_draft
from .telemetry import Tracer, utcnow
from .tools import Tools, SCHEMAS
from .real_tools import RealTools, REAL_SCHEMAS


@dataclass(frozen=True)
class Limits:
    max_steps: int = 10
    max_tool_calls: int = 12
    timeout_s: float = 180
    model_timeout_s: float = 45
    max_tokens: int = 24000
    retry_delay_s: float = 0.05


class BudgetExceeded(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


class TripExecution:
    """One shared execution kernel; runtime dependencies stay outside snapshots.

    A model turn and its dispatch are explicit boundaries. Tool retries remain
    inside dispatch, so advancing never pauses in the middle of a retry loop.
    """

    def __init__(self, scenario, provider, store, limits=None, otel=None,
                 evaluation_id=None, *, tool_mode="demo", policy_path=None,
                 transport_http=None):
        if tool_mode not in {"demo", "real"}:
            raise ValueError("Unknown tool mode")
        limits = limits or Limits()
        tracer = Tracer(store, otel)
        request = normalize_request(scenario["request"], tool_mode)
        toolbox = RealTools(tracer.trace_id, policy_path, request=request,
                            evidence_dir=store.path.parent / "evidence" / tracer.trace_id, transport_http=transport_http) if tool_mode == "real" else Tools(scenario, request)
        schemas = REAL_SCHEMAS if tool_mode == "real" else SCHEMAS
        prompt = (PACKAGE / "prompts" / ("real_planner.txt" if tool_mode == "real" else "planner.txt")).read_text(encoding="utf-8")
        started = time.perf_counter()
        deadline = started + limits.timeout_s
        run = {"trace_id": tracer.trace_id, "started_at": utcnow(), "scenario": scenario["id"],
               "provider": provider.name, "model": provider.model, "app_version": __version__,
               "prompt_hash": fingerprint(prompt), "dataset_hash": fingerprint([scenario, toolbox._data]),
               "tools_hash": fingerprint(schemas), "evaluation_id": evaluation_id, "tool_mode": tool_mode,
               "status": "running", "termination": None, "input_tokens": 0, "output_tokens": 0,
               "usage_complete": True, "cost_usd": 0 if provider.name == "baseline" or getattr(provider, "pricing", None) else None,
               "pricing_usd_per_million": getattr(provider, "pricing", None), "cache_hit_tokens": 0,
               "limits": vars(limits), "tool_calls": 0, "model_calls": 0, "retries": 0,
               "guardrail_blocks": 0, "observed_evidence_ids": []}
        store.save_run(run)
        messages = [{"role": "system", "content": prompt},
                    {"role": "user", "content": json.dumps(request, ensure_ascii=False)}]
        verdict = handoff()
        tool_results = {}

        self.scenario = scenario
        self.provider = provider
        self.store = store
        self.limits = limits
        self.tracer = tracer
        self.request = request
        self.toolbox = toolbox
        self.schemas = schemas
        self.prompt = prompt
        self.started = started
        self.deadline = deadline
        self.run = run
        self.messages = messages
        self.verdict = verdict
        self.tool_results = tool_results
        self.tool_mode = tool_mode
        self.stage = "model_turn"
        self.pending_call = None
        self.wall_started = time.time()
        self.restored = False

    def check_budget(self):
        if time.perf_counter() >= self.deadline:
            raise BudgetExceeded("deadline")
        if self.run["input_tokens"] + self.run["output_tokens"] >= self.limits.max_tokens:
            raise BudgetExceeded("tokens")

    def model_turn(self):
        if self.run["model_calls"] >= self.limits.max_steps:
            raise BudgetExceeded("steps")
        self.check_budget()
        with self.tracer.span("policy_decision" if self.provider.name == "baseline" else "chat", step=self.run["model_calls"] + 1) as attrs:
            self.run["model_calls"] += 1
            try:
                response = self.provider.chat(self.messages, self.schemas,
                                         min(self.limits.model_timeout_s, self.deadline - time.perf_counter()))
            except Exception:
                self.run["usage_complete"] = False
                self.run["cost_usd"] = None
                raise
            for key in ("input_tokens", "output_tokens"):
                count = response.get(key)
                if type(count) is int and count >= 0:
                    self.run[key] += count
                    attrs[key] = count
                else:
                    self.run["usage_complete"] = False
            if self.run["cost_usd"] is not None and self.provider.name != "baseline":
                cost = response.get("cost_usd")
                self.run["cost_usd"] = self.run["cost_usd"] + cost if cost is not None else None
            if type(response.get("cache_hit_tokens")) is int:
                self.run["cache_hit_tokens"] += response["cache_hit_tokens"]
                attrs["cache_hit_tokens"] = response["cache_hit_tokens"]
            message = response["message"]
            calls = message.get("tool_calls", [])
            if not isinstance(calls, list) or len(calls) != 1:
                raise ValueError("Exactly one tool call per step required")
            call = calls[0]["function"]
            name, args = call["name"], call["arguments"]
            if not isinstance(name, str):
                raise ValueError("Invalid tool name")
            # Name is constrained before it can enter telemetry.
            if name not in {s["function"]["name"] for s in self.schemas}:
                self.run["guardrail_blocks"] += 1
                raise ValueError("Unknown tool")
            attrs["selected_tool"] = name
        self.check_budget()
        self.messages.append(message)
        self.pending_call = {"name": name, "args": args, "call": calls[0]}
        self.stage = "dispatch"

    def dispatch(self):
        self.check_budget()
        name, args = self.pending_call["name"], self.pending_call["args"]
        if name == "finish":
            with self.tracer.span("validate_result"):
                try:
                    self.verdict = validate_verdict(args, self.toolbox.seen, self.request, self.tool_results, self.tool_mode)
                except ValueError:
                    self.run["guardrail_blocks"] += 1
                    raise
            self.run["termination"] = "completed"
            self.stage = "finalize"
            self.pending_call = None
            return
        result = None
        for attempt in range(2):
            self.check_budget()
            if self.toolbox.calls >= self.limits.max_tool_calls:
                raise BudgetExceeded("tool_calls")
            try:
                with self.tracer.span("execute_tool", tool=name, attempt=attempt + 1) as attrs:
                    result = (self.toolbox.execute(name, args, timeout=max(0.001, self.deadline - time.perf_counter()))
                              if self.tool_mode == "real" else self.toolbox.execute(name, args))
                    attrs["record_count"] = len(result.get("records", []))
                    if self.tool_mode == "real":
                        attrs["call_id"] = result["call_id"]
                        attrs["tool_status"] = result["status"]
                        if result["status"] == "ok" and name == "get_policy_snapshot":
                            attrs["content_hash"] = result["data"]["content_hash"]
                            attrs["source_ref"] = result["data"]["source_ref"]
                        elif result["status"] == "error":
                            attrs["error.type"] = result["error"]["type"]
                        if name == "search_transport":
                            attrs["http_requests"] = result["http_requests"]
                            attrs["attempt"] = result["attempt"]
                            if result["retry_of"] is not None:
                                self.run["retries"] += 1
                                attrs["retry_of"] = result["retry_of"]
                            if result.get("evidence_error"):
                                attrs["evidence_error.type"] = result["evidence_error"]["type"]
                            if result["data"] is not None:
                                attrs["query_date"] = result["data"]["departure_date"]
                                attrs["mode"] = result["data"]["mode"]
                                attrs["record_count"] = result["data"]["returned_count"]
                break
            except TimeoutError:
                if attempt == 0:
                    self.run["retries"] += 1
                    time.sleep(min(self.limits.retry_delay_s, max(0, self.deadline - time.perf_counter())))
                else:
                    result = {"error": "tool_timeout", "records": []}
            except ValueError:
                self.run["guardrail_blocks"] += 1
                raise
        self.tool_results[result["call_id"] if self.tool_mode == "real" else name] = result
        tool_message = {"role": "tool", "tool_name": name,
                        "content": json.dumps(result, ensure_ascii=False)}
        if "id" in self.pending_call["call"]:
            tool_message["tool_call_id"] = self.pending_call["call"]["id"]
        self.messages.append(tool_message)
        self.pending_call = None
        self.stage = "model_turn"

    def advance(self):
        """Advance one boundary; callers classify failures with fail()."""
        if self.stage == "model_turn":
            self.model_turn()
        elif self.stage == "dispatch":
            self.dispatch()

    def snapshot(self):
        """Private JSON data only; never expose this through public trace APIs."""
        return copy.deepcopy({
            "schema_version": 1, "stage": self.stage,
            "scenario": self.scenario, "request": self.request,
            "tool_mode": self.tool_mode, "run": self.run,
            "messages": self.messages, "verdict": self.verdict,
            "tool_results": self.tool_results, "pending_call": self.pending_call,
            "tool_state": self.toolbox.snapshot(),
            "active_s": max(0, time.perf_counter() - self.started),
            "wall_started": self.wall_started,
            "policy_path": str(self.toolbox.policy_path.resolve()) if self.tool_mode == "real" else None,
            "policy_hash": self.policy_hash(),
        })

    def policy_hash(self):
        if self.tool_mode != "real":
            return None
        try:
            return fingerprint(self.toolbox.policy_path.read_text(encoding="utf-8"))
        except OSError:
            return None

    @classmethod
    def restore(cls, state, provider, store, *, otel=None, policy_path=None, transport_http=None):
        if state.get("schema_version") != 1:
            raise ValueError("Incompatible execution snapshot")
        state = copy.deepcopy(state)
        run = state["run"]
        if (provider.name, provider.model, getattr(provider, "pricing", None)) != (
                run["provider"], run["model"], run["pricing_usd_per_million"]):
            raise ValueError("Incompatible provider configuration")
        self = cls.__new__(cls)
        for key in ("stage", "scenario", "request", "tool_mode", "run", "messages",
                    "verdict", "tool_results", "pending_call"):
            setattr(self, key, state[key])
        self.provider, self.store = provider, store
        self.limits = Limits(**run["limits"])
        self.tracer = Tracer(store, otel)
        self.tracer.trace_id = run["trace_id"]
        self.schemas = REAL_SCHEMAS if self.tool_mode == "real" else SCHEMAS
        self.prompt = (PACKAGE / "prompts" / ("real_planner.txt" if self.tool_mode == "real" else "planner.txt")).read_text(encoding="utf-8")
        if fingerprint(self.schemas) != run["tools_hash"] or fingerprint(self.prompt) != run["prompt_hash"]:
            raise ValueError("Incompatible prompt or tool schema")
        if policy_path is not None and str(Path(policy_path).resolve()) != state["policy_path"]:
            raise ValueError("Incompatible policy source")
        self.toolbox = (RealTools(self.tracer.trace_id, state["policy_path"], request=self.request,
            evidence_dir=store.path.parent / "evidence" / self.tracer.trace_id,
            transport_http=transport_http) if self.tool_mode == "real" else Tools(self.scenario, self.request))
        if fingerprint([self.scenario, self.toolbox._data]) != run["dataset_hash"]:
            raise ValueError("Incompatible tool data")
        self.toolbox.restore(state["tool_state"])
        if self.policy_hash() != state["policy_hash"]:
            raise ValueError("Incompatible policy content")
        self.started = time.perf_counter() - state["active_s"]
        self.deadline = self.started + self.limits.timeout_s
        self.wall_started = state["wall_started"]
        self.restored = True
        return self

    def fail(self, exc):
        self.stage = "finalize"
        self.verdict = handoff()
        if isinstance(exc, BudgetExceeded):
            self.run["termination"] = "budget_exceeded"
            self.run["budget_reason"] = exc.reason
        elif isinstance(exc, (ValueError, TypeError, KeyError, IndexError)):
            self.run["termination"] = "invalid_response"
        else:
            self.run["termination"] = "provider_error"
            self.run["error_type"] = type(exc).__name__
            if type(getattr(exc, "http_status", None)) is int:
                self.run["http_status"] = exc.http_status

    def finalize(self):
        self.run.update({"status": self.verdict["status"], "verdict": self.verdict,
                    "draft": format_draft(self.verdict), "tool_calls": self.toolbox.calls,
                    "observed_evidence_ids": sorted(self.toolbox.seen),
                    "observed_tool_names": sorted({r["tool"] for r in self.tool_results.values()}) if self.tool_mode == "real" else sorted(self.tool_results),
                    "duration_ms": round(((time.time() - self.wall_started) if self.restored else (time.perf_counter() - self.started)) * 1000, 3),
                    "finished_at": utcnow()})
        if self.tool_mode == "real":
            self.run["request"] = self.request
            self.run["http_requests"] = self.toolbox.http_requests
            self.run["evidence"] = list(self.toolbox.seen.values())
            self.run["tool_results"] = list(self.toolbox.history.values())
        self.store.save_run(self.run)
        self.stage = "done"
        return self.run


def plan_trip(scenario, provider, store, limits=None, otel=None, evaluation_id=None, *, tool_mode="demo", policy_path=None, transport_http=None):
    execution = TripExecution(scenario, provider, store, limits, otel, evaluation_id,
                              tool_mode=tool_mode, policy_path=policy_path,
                              transport_http=transport_http)
    try:
        with execution.tracer.span("invoke_agent", provider=provider.name, model=provider.model) as attrs:
            while execution.stage != "finalize":
                execution.advance()
            attrs["outcome"] = execution.verdict["status"]
    except Exception as exc:
        execution.fail(exc)
    return execution.finalize()
