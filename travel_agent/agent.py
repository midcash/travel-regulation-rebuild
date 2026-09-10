from __future__ import annotations

from dataclasses import dataclass
import json
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


def plan_trip(scenario, provider, store, limits=None, otel=None, evaluation_id=None, *, tool_mode="demo", policy_path=None, transport_http=None):
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

    def check_budget():
        if time.perf_counter() >= deadline:
            raise BudgetExceeded("deadline")
        if run["input_tokens"] + run["output_tokens"] >= limits.max_tokens:
            raise BudgetExceeded("tokens")

    try:
        with tracer.span("invoke_agent", provider=provider.name, model=provider.model) as root_attrs:
            for step in range(limits.max_steps):
                check_budget()
                with tracer.span("policy_decision" if provider.name == "baseline" else "chat", step=step + 1) as attrs:
                    run["model_calls"] += 1
                    try:
                        response = provider.chat(messages, schemas,
                                                 min(limits.model_timeout_s, deadline - time.perf_counter()))
                    except Exception:
                        run["usage_complete"] = False
                        run["cost_usd"] = None
                        raise
                    for key in ("input_tokens", "output_tokens"):
                        count = response.get(key)
                        if type(count) is int and count >= 0:
                            run[key] += count
                            attrs[key] = count
                        else:
                            run["usage_complete"] = False
                    if run["cost_usd"] is not None and provider.name != "baseline":
                        cost = response.get("cost_usd")
                        run["cost_usd"] = run["cost_usd"] + cost if cost is not None else None
                    if type(response.get("cache_hit_tokens")) is int:
                        run["cache_hit_tokens"] += response["cache_hit_tokens"]
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
                    if name not in {s["function"]["name"] for s in schemas}:
                        run["guardrail_blocks"] += 1
                        raise ValueError("Unknown tool")
                    attrs["selected_tool"] = name
                check_budget()
                messages.append(message)
                if name == "finish":
                    with tracer.span("validate_result"):
                        try:
                            verdict = validate_verdict(args, toolbox.seen, request, tool_results, tool_mode)
                        except ValueError:
                            run["guardrail_blocks"] += 1
                            raise
                    run["termination"] = "completed"
                    root_attrs["outcome"] = verdict["status"]
                    break
                result = None
                for attempt in range(2):
                    check_budget()
                    if toolbox.calls >= limits.max_tool_calls:
                        raise BudgetExceeded("tool_calls")
                    try:
                        with tracer.span("execute_tool", tool=name, attempt=attempt + 1) as attrs:
                            result = (toolbox.execute(name, args, timeout=max(0.001, deadline - time.perf_counter()))
                                      if tool_mode == "real" else toolbox.execute(name, args))
                            attrs["record_count"] = len(result.get("records", []))
                            if tool_mode == "real":
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
                                        run["retries"] += 1
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
                            run["retries"] += 1
                            time.sleep(min(limits.retry_delay_s, max(0, deadline - time.perf_counter())))
                        else:
                            result = {"error": "tool_timeout", "records": []}
                    except ValueError:
                        run["guardrail_blocks"] += 1
                        raise
                tool_results[result["call_id"] if tool_mode == "real" else name] = result
                tool_message = {"role": "tool", "tool_name": name,
                                "content": json.dumps(result, ensure_ascii=False)}
                if "id" in calls[0]:
                    tool_message["tool_call_id"] = calls[0]["id"]
                messages.append(tool_message)
            else:
                raise BudgetExceeded("steps")
    except BudgetExceeded as exc:
        run["termination"] = "budget_exceeded"
        run["budget_reason"] = exc.reason
        verdict = handoff()
    except (ValueError, TypeError, KeyError, IndexError):
        run["termination"] = "invalid_response"
        verdict = handoff()
    except Exception as exc:
        # Do not persist exceptions: upstream messages can contain secrets/URLs.
        run["termination"] = "provider_error"
        run["error_type"] = type(exc).__name__
        if type(getattr(exc, "http_status", None)) is int:
            run["http_status"] = exc.http_status
        verdict = handoff()
    run.update({"status": verdict["status"], "verdict": verdict,
                "draft": format_draft(verdict), "tool_calls": toolbox.calls,
                "observed_evidence_ids": sorted(toolbox.seen),
                "observed_tool_names": sorted({r["tool"] for r in tool_results.values()}) if tool_mode == "real" else sorted(tool_results),
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                "finished_at": utcnow()})
    if tool_mode == "real":
        run["request"] = request
        run["http_requests"] = toolbox.http_requests
        run["evidence"] = list(toolbox.seen.values())
        run["tool_results"] = list(toolbox.history.values())
    store.save_run(run)
    return run
