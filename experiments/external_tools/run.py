"""PyCharm 可直接运行；也可以 python -m experiments.external_tools.run。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, time, timedelta, timezone, date
from decimal import Decimal
from pathlib import Path
from time import perf_counter

import httpx

# 直接运行文件时定位当前仓库，绝不添加原 skill 仓库到 sys.path。
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from experiments.external_tools.amap import AmapRouteProvider
from experiments.external_tools.clock import SystemClock
from experiments.external_tools.config import load_settings
from experiments.external_tools.errors import ToolError
from experiments.external_tools.models import RouteQuery, StayQuery, TransportQuery
from experiments.external_tools.policy import PolicyDocumentError, PolicySnapshotLoader
from experiments.external_tools.tuniu import TuniuTravelProvider

HERE = Path(__file__).resolve().parent
POLICY_PATH = HERE / "policy.md"
CHINA = timezone(timedelta(hours=8))


async def run(args: argparse.Namespace) -> bool:
    """每项独立记录成功/失败；任一项失败则整个运行返回非零退出码。"""
    settings = load_settings(args.env_file)
    day = args.date or (datetime.now(CHINA).date() + timedelta(days=3))
    timeout = Decimal(str(args.timeout))
    if not timeout.is_finite() or timeout <= 0:
        raise ValueError("timeout must be positive and finite")
    run_id = datetime.now(CHINA).strftime("%Y%m%dT%H%M%S%f")
    folder = HERE / "results" / run_id
    folder.mkdir(parents=True)
    queries = {
        "policy": POLICY_PATH,
        "flight": TransportQuery(
            query_id="lab-flight",
            trace_id=run_id,
            origin=args.origin,
            destination=args.destination,
            departure_after=datetime.combine(day, time.min, CHINA),
            max_results=3,
        ),
        "train": TransportQuery(
            query_id="lab-train",
            trace_id=run_id,
            origin=args.origin,
            destination=args.destination,
            mode="train",
            service_class="high_speed_rail",
            departure_after=datetime.combine(day, time.min, CHINA),
            max_results=3,
        ),
        "route": RouteQuery(
            query_id="lab-route",
            trace_id=run_id,
            origin={"longitude": 121.443, "latitude": 31.197},
            destination={"longitude": 121.499, "latitude": 31.239},
        ),
        "hotel": StayQuery(
            query_id="lab-hotel",
            trace_id=run_id,
            destination=args.destination,
            date_range={"start": day, "end": day + timedelta(days=1)},
            travelers=1,
            max_results=3,
        ),
    }
    reports = []
    async with httpx.AsyncClient(follow_redirects=False) as client:
        for name, query in queries.items():
            if args.tool != "all" and args.tool != name:
                continue
            started = perf_counter()
            report = {
                "tool": name,
                "observed_at": datetime.now(CHINA).isoformat(),
                "input": str(query)
                if isinstance(query, Path)
                else query.model_dump(mode="json"),
            }
            try:
                if name == "policy":
                    result = PolicySnapshotLoader().load(query)
                else:
                    provider = (
                        AmapRouteProvider if name == "route" else TuniuTravelProvider
                    )(settings, client, SystemClock())
                    # httpx timeout 控制各网络阶段；这里另设整次调用的墙钟上限。
                    async with asyncio.timeout(float(timeout)):
                        result = await provider.search(query, timeout_seconds=timeout)
                report.update(status="ok", result=result.model_dump(mode="json"))
                count = len(result.rules if name == "policy" else result.items)
                print(f"{name}: OK ({count} items)", flush=True)
            except (ToolError, PolicyDocumentError, TimeoutError) as exc:
                report.update(
                    status="error",
                    error={
                        "type": type(exc).__name__,
                        "message": str(exc) or "total call deadline exceeded",
                    },
                )
                print(
                    f"{name}: ERROR ({type(exc).__name__}: {report['error']['message']})",
                    flush=True,
                )
            report["elapsed_seconds"] = round(perf_counter() - started, 3)
            (folder / f"{name}.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            reports.append(report)
    success = all(r["status"] == "ok" for r in reports)
    (folder / "summary.json").write_text(
        json.dumps({"success": success, "runs": reports}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Results: {folder}", flush=True)
    return success


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tool",
        choices=["all", "policy", "flight", "train", "route", "hotel"],
        default="all",
    )
    parser.add_argument(
        "--date", type=date.fromisoformat, help="YYYY-MM-DD，默认中国时间今天+3天"
    )
    parser.add_argument("--origin", default="北京")
    parser.add_argument("--destination", default="上海")
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(run(args)) else 1)


if __name__ == "__main__":
    main()
