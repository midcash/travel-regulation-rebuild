from __future__ import annotations

import argparse
import json
from pathlib import Path

if __package__:
    from .runtime import HilError, HilStore, TravelHilLoop
else:
    from runtime import HilError, HilStore, TravelHilLoop


DEFAULT_DB = Path(__file__).parent / "artifacts" / "hil.sqlite3"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="最小 Human-in-the-loop Agent Loop 实验")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 文件路径")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("start", help="启动一次差旅规划")
    status = sub.add_parser("status", help="查看运行状态")
    status.add_argument("run_id")
    resume = sub.add_parser("resume", help="模拟进程重启后读取等待状态")
    resume.add_argument("run_id")
    respond = sub.add_parser("respond", help="提交人工回复")
    respond.add_argument("interaction_id")
    respond.add_argument("--action", required=True, choices=["select", "approve", "revise", "cancel"])
    respond.add_argument("--transport-id")
    respond.add_argument("--stay-id")
    respond.add_argument("--budget", type=int)
    respond.add_argument("--request-id")
    respond.add_argument("--state-version", type=int)

    args = parser.parse_args(argv)
    store = HilStore(args.db)
    loop = TravelHilLoop(store)
    try:
        if args.command is None:
            if __package__:
                from .interactive import run_menu
            else:
                from interactive import run_menu
            return run_menu(loop)
        if args.command == "start":
            result = loop.start()
        elif args.command == "status":
            result = store.get_run(args.run_id)
        elif args.command == "resume":
            result = loop.resume(args.run_id)
        else:
            payload = {}
            if args.transport_id or args.stay_id:
                payload.update({"transport_id": args.transport_id, "stay_id": args.stay_id})
            if args.budget is not None:
                payload["request_patch"] = {"budget_cny": args.budget}
            result = loop.respond(
                args.interaction_id,
                args.action,
                payload,
                response_id=args.request_id,
                expected_state_version=args.state_version,
            )
    except HilError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
