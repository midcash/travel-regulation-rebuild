"""Deterministic outcome graders. Oracles never enter the agent/tool interface."""
from __future__ import annotations

import json
import math
from pathlib import Path
import uuid

from .agent import plan_trip
from .domain import fingerprint, scenario_by_id
from datetime import datetime
from .telemetry import utcnow

ROOT = Path(__file__).resolve().parent.parent


def grade(run, case):
    """Independent assertions against oracle values, not the runtime validator."""
    verdict = run.get("verdict", {})
    refs = verdict.get("evidence_ids", [])
    seen = run.get("observed_evidence_ids", [])
    legs = verdict.get("itinerary", [])
    planned = case["status"] == "planned"
    schedule_ok = False
    budget_ok = False
    try:
        if planned and len(legs) == 4:
            starts = [datetime.fromisoformat(x["start_at"]) for x in legs]
            ends = [datetime.fromisoformat(x["end_at"]) for x in legs]
            schedule_ok = (legs[0]["start_at"] == case["departure_at"]
                and legs[-1]["end_at"] == case["arrival_at"]
                and ends[-1] <= datetime.fromisoformat(case["arrival_deadline"])
                and all(a < b for a, b in zip(starts, ends))
                and all(ends[i] <= starts[i+1] and legs[i]["to"] == legs[i+1]["from"] for i in range(3))
                and legs[0]["from"] == "杭州东站" and legs[-1]["to"] == "东方明珠塔")
            prices = [x["price_cny"] for x in legs]
            budget_ok = (all(type(x) is int and x >= 0 for x in prices)
                and sum(prices) == case["total_cny"] == verdict.get("total_cny")
                and sum(prices) <= case["trip_max_cny"] and legs[2]["price_cny"] <= case["hotel_max_cny"])
        elif not planned:
            schedule_ok = not legs and not verdict.get("segment_ids")
            budget_ok = verdict.get("total_cny") is None
    except (KeyError, ValueError, TypeError, IndexError):
        pass
    checks = {
        "completed": run.get("termination") == "completed",
        "reason": verdict.get("reason") == case["reason"],
        "status": verdict.get("status") == case["status"],
        "required_evidence": set(case["required_evidence"]).issubset(refs),
        "grounded_citations": set(refs).issubset(seen) and len(refs) == len(set(refs)),
        "required_tools": set(case["required_tools"]).issubset(run.get("observed_tool_names", [])),
        "canonical_records": legs == case["expected_itinerary"],
        "itinerary": verdict.get("segment_ids") == case["segment_ids"]
                     and [x.get("id") for x in legs] == case["segment_ids"],
        "schedule": schedule_ok,
        "budget": budget_ok,
        "retry_count": run.get("retries") == case["expected_retries"],
        "safe_failure": planned or verdict.get("status") == case["status"] and not legs,
        "no_guardrail_violation": run.get("guardrail_blocks", 0) == 0,
    }
    safe = all(checks[x] for x in ("grounded_citations", "required_tools", "canonical_records", "schedule", "budget", "safe_failure", "no_guardrail_violation"))
    return {"passed": all(checks.values()), "safe": safe, "checks": checks,
            "failures": [key for key, value in checks.items() if not value]}


def percentile(values, fraction):
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


def evaluate(store, provider, split="all", repeats=1, cases_path=None, gates_path=None, otel=None):
    if split not in {"all", "dev", "test"} or not 1 <= repeats <= 10:
        raise ValueError("Invalid split/repeats")
    cases = json.loads(Path(cases_path or ROOT / "evals/cases.json").read_text(encoding="utf-8"))
    cases = [c for c in cases if split == "all" or c["split"] == split]
    if not cases:
        raise ValueError("No evaluation cases selected")
    gates = json.loads(Path(gates_path or ROOT / "evals/gates.json").read_text(encoding="utf-8"))
    report = {"id": uuid.uuid4().hex, "started_at": utcnow(), "provider": provider.name,
              "model": provider.model, "split": split, "repeats": repeats,
              "oracle_hash": fingerprint(cases), "gates": gates, "rows": []}
    for case in cases:
        for trial in range(repeats):
            run = plan_trip(scenario_by_id(case["scenario"]), provider, store,
                              otel=otel, evaluation_id=report["id"])
            report["rows"].append({"scenario": case["scenario"], "split": case["split"],
                "trial": trial + 1, "trace_id": run["trace_id"], "expected": case,
                "actual": run["verdict"], "termination": run["termination"],
                "duration_ms": run["duration_ms"], "tool_calls": run["tool_calls"],
                "input_tokens": run["input_tokens"], "output_tokens": run["output_tokens"],
                "usage_complete": run["usage_complete"], **grade(run, case)})
            report["rows"][-1]["cost_usd"] = run["cost_usd"]
    rows = report["rows"]
    total = len(rows)
    report.update({"total": total, "passed": sum(x["passed"] for x in rows),
        "pass_rate": sum(x["passed"] for x in rows) / total,
        "safety_rate": sum(x["safe"] for x in rows) / total,
        "p95_latency_ms": percentile([x["duration_ms"] for x in rows], .95),
        "max_tool_calls": max(x["tool_calls"] for x in rows),
        "case_consistency_rate": sum(all(r["passed"] for r in rows if r["scenario"] == c["scenario"])
                                      for c in cases) / len(cases),
        "input_tokens": sum(x["input_tokens"] for x in rows),
        "output_tokens": sum(x["output_tokens"] for x in rows),
        "usage_complete": all(x["usage_complete"] for x in rows),
        "cost_usd": sum(x["cost_usd"] for x in rows) if all(x["cost_usd"] is not None for x in rows) else None,
        "note": "Synthetic cases; deterministic baseline is not evidence of LLM quality. Test split is a regression split, not an independently curated benchmark.",
        "finished_at": utcnow()})
    report["gate_checks"] = {
        "pass_rate": report["pass_rate"] >= gates["min_pass_rate"],
        "safety_rate": report["safety_rate"] >= gates["min_safety_rate"],
        "latency": report["p95_latency_ms"] <= gates["max_p95_latency_ms"],
        "tool_budget": report["max_tool_calls"] <= gates["max_tool_calls"],
    }
    report["gate_passed"] = all(report["gate_checks"].values())
    store.save_evaluation(report)
    return report
