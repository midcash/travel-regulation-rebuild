"""实验四的命令行入口。

无参数进中文菜单；带子命令输出 JSON，供脚本和测试使用——
用例 2（真正跨进程恢复）就是靠 `start` 在一个独立 Python 进程里跑出暂停点，
再由测试进程从数据库冷启动继续。

    python -m experiments.experiment_4_agent_hil.cli start --db <路径> --script happy
    python -m experiments.experiment_4_agent_hil.cli resume --db <路径> --run <run_id>
    python -m experiments.experiment_4_agent_hil.cli respond --db <路径> \
        --interaction <id> --action confirm --text "可以"
    python -m experiments.experiment_4_agent_hil.cli status  --db <路径> --run <run_id>
    python -m experiments.experiment_4_agent_hil.cli events  --db <路径> --run <run_id>
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__:
    from . import config as C
    from .loop import AgentHilLoop, HilError
    from .provider import ScriptExhausted
    from .snapshot import TERMINAL, WAITING
    from .store import AgentHilStore, StoreError
else:  # 允许直接 python cli.py 运行，与 exp3 一致
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from experiment_4_agent_hil import config as C
    from experiment_4_agent_hil.loop import AgentHilLoop, HilError
    from experiment_4_agent_hil.provider import ScriptExhausted
    from experiment_4_agent_hil.snapshot import TERMINAL, WAITING
    from experiment_4_agent_hil.store import AgentHilStore, StoreError

DEFAULT_DB = Path(__file__).parent / "artifacts" / "exp4.sqlite3"

STATUS_CN = {"running": "执行中", "waiting_for_human": "等待人工回复",
             "completed": "已完成", "failed": "执行失败",
             "cancelled": "已取消", "expired": "已过期"}


def build(args):
    return AgentHilLoop(AgentHilStore(args.db))


def dump(result):
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))


def cmd_start(args):
    loop = build(args)
    script = C.SCRIPTS[args.script]
    result = loop.start(C.SCENARIO, C.config(script), C.limits_dict(
        **({"max_human_requests": args.max_human} if args.max_human else {})), C.REQUEST)
    dump(result)
    return 0


def cmd_resume(args):
    dump(build(args).resume(args.run))
    return 0


def cmd_respond(args):
    response = {"action": args.action}
    if args.text:
        response["text"] = args.text
    dump(build(args).respond(args.interaction, response, response_id=args.response_id))
    return 0


def cmd_status(args):
    store = AgentHilStore(args.db)
    snapshot = store.load_snapshot(args.run)
    dump({"run_id": snapshot["run_id"], "status": snapshot["status"],
          "stage": snapshot["stage"], "step": snapshot["step"],
          "human_requests": snapshot["human_requests"],
          "elapsed_s": round(snapshot["elapsed_s"], 3),
          "verdict": snapshot["verdict"], "run": snapshot["run"]})
    return 0


def cmd_events(args):
    dump(AgentHilStore(args.db).list_events(args.run))
    return 0


# ---------------- 中文菜单 ----------------

def ask(prompt, count):
    while True:
        value = input(prompt).strip().lower()
        if value in {"q", "0"}:
            return None
        if value.isascii() and value.isdigit() and 1 <= int(value) <= count:
            return int(value) - 1
        print(f"请输入 1–{count}，或输入 0 返回。")


def show(result):
    print(f"\n── 运行 {result['run_id'][:8]} ──")
    print(f"状态：{STATUS_CN.get(result['status'], result['status'])}"
          f"；已用回合 {result['model_calls']}；工具调用 {result['tool_calls']}"
          f"；活跃耗时 {result['elapsed_s']} 秒；人工请求 {result['human_requests']} 次")
    if result["status"] == WAITING:
        request = result["human_request"]
        print(f"\n助手提问：{request['payload']['question']}")
        for i, option in enumerate(request["payload"]["options"], 1):
            print(f"  {i}. {option}")
    if result.get("verdict") and result["verdict"]["reason"] != "execution_failed":
        print(f"结论：{result['verdict']['status']} / {result['verdict']['reason']}")
        for claim in result["verdict"]["claims"]:
            print(f"  · {claim['label']}")
    if result["status"] == WAITING:
        print(f"本次确认有效至：{result['human_request']['expires_at'][:19]} UTC")


def menu(args):
    loop = build(args)
    print("实验四：Agent loop × human-in-the-loop")
    print("每次操作都会落库；可以在等待时退出，下次继续。")
    while True:
        print("\n  1. 新建运行（脚本 happy）\n  2. 新建运行（脚本 two-asks）"
              "\n  3. 继续已有运行\n  4. 查看事件\n  0. 退出")
        choice = ask("你 > ", 4)
        if choice is None:
            return 0
        try:
            if choice == 0:
                result = loop.start(C.SCENARIO, C.config(C.SCRIPTS["happy"]),
                                    C.limits_dict(), C.REQUEST)
            elif choice == 1:
                result = loop.start(C.SCENARIO, C.config(C.SCRIPTS["two-asks"]),
                                    C.limits_dict(), C.REQUEST)
            elif choice == 2:
                runs = [r for r in loop.store.list_runs() if r["status"] not in TERMINAL]
                if not runs:
                    print("没有进行中的运行。")
                    continue
                for i, row in enumerate(runs, 1):
                    print(f"  {i}. {row['created_at'][:19]} · "
                          f"{STATUS_CN.get(row['status'], row['status'])} · {row['run_id'][:8]}")
                index = ask("选择运行编号（0 返回）：", len(runs))
                if index is None:
                    continue
                result = loop.resume(runs[index]["run_id"])
            else:
                runs = loop.store.list_runs()
                if not runs:
                    print("暂无运行。")
                    continue
                for i, row in enumerate(runs, 1):
                    print(f"  {i}. {row['created_at'][:19]} · {row['run_id'][:8]}")
                index = ask("选择运行编号（0 返回）：", len(runs))
                if index is None:
                    continue
                for entry in loop.store.list_events(runs[index]["run_id"]):
                    print(f"  {entry['created_at'][:19]} · {entry['type']}")
                continue
            show(result)
            while result["status"] == WAITING:
                request = result["human_request"]
                text = input("你的回复（直接回车表示同意，q 保存并返回）：").strip()
                if text.lower() == "q":
                    break
                result = loop.respond(request["interaction_id"],
                                      {"action": "reply", "text": text})
                show(result)
        except (HilError, StoreError, C.ScriptExhausted) as exc:
            print(f"操作未完成：{exc}")
        except (EOFError, KeyboardInterrupt):
            print("\n已退出，进度已保存。")
            return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="实验四：Agent loop 与 HITL 的整合")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("start"); p.add_argument("--script", default="happy", choices=sorted(C.SCRIPTS))
    p.add_argument("--max-human", type=int); p.set_defaults(func=cmd_start)
    p = sub.add_parser("resume"); p.add_argument("--run", required=True); p.set_defaults(func=cmd_resume)
    p = sub.add_parser("respond"); p.add_argument("--interaction", required=True)
    p.add_argument("--action", default="reply"); p.add_argument("--text", default="")
    p.add_argument("--response-id"); p.set_defaults(func=cmd_respond)
    p = sub.add_parser("status"); p.add_argument("--run", required=True); p.set_defaults(func=cmd_status)
    p = sub.add_parser("events"); p.add_argument("--run", required=True); p.set_defaults(func=cmd_events)

    args = parser.parse_args(argv)
    if args.command is None:
        return menu(args)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
