from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .agent import plan_trip
from .domain import scenario_by_id, scenarios, custom_scenario
from .evaluation import evaluate
from .providers import make_provider
from .telemetry import Store, make_otel
from .settings import load_env


def main():
    load_env()
    parser = argparse.ArgumentParser(description="Travel Agent Lab")
    parser.add_argument("--db", default="artifacts/observability.sqlite3")
    parser.add_argument("--otel-endpoint", default=os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("scenarios")
    for name in ("run", "eval", "serve"):
        child = sub.add_parser(name)
        child.add_argument("--provider", choices=["baseline", "ollama", "deepseek"], default="deepseek")
        child.add_argument("--model", default=None)
        child.add_argument("--base-url", default=None)
        if name == "run":
            child.add_argument("--scenario", default="hangzhou-shanghai")
            child.add_argument("--request-file", help="UTF-8 JSON structured request")
            child.add_argument("--tool-mode", choices=["demo", "real"], default="demo")
        elif name == "eval":
            child.add_argument("--split", choices=["all", "dev", "test"], default="all")
            child.add_argument("--repeats", type=int, choices=range(1, 11), default=1)
            child.add_argument("--output", default="artifacts/latest-evaluation.json")
        else:
            child.add_argument("--port", type=int, default=8766)
    trace = sub.add_parser("trace")
    trace.add_argument("trace_id")
    args = parser.parse_args()
    if args.command == "scenarios":
        print(json.dumps([{"id": x["id"], "label": x["label"], "request": x["request"]} for x in scenarios()], ensure_ascii=False, indent=2))
        return 0
    store = Store(args.db)
    if args.command == "trace":
        result = store.get_run(args.trace_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result else 1
    otel, owner = make_otel(args.otel_endpoint)
    try:
        provider = make_provider(args.provider, args.model, args.base_url)
        if args.command == "serve":
            from .server import serve
            serve(store, provider, args.port, otel)
            return 0
        if args.command == "run":
            scenario = (custom_scenario(json.loads(Path(args.request_file).read_text(encoding="utf-8-sig")))
                        if args.request_file else scenario_by_id(args.scenario))
            result = plan_trip(scenario, provider, store, otel=otel, tool_mode=args.tool_mode)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["termination"] == "completed" else 1
        report = evaluate(store, provider, args.split, args.repeats, otel=otel)
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({k: report[k] for k in ("id", "provider", "total", "passed", "pass_rate", "safety_rate", "gate_passed")}, indent=2))
        return 0 if report["gate_passed"] else 1
    finally:
        if owner:
            owner.shutdown()
