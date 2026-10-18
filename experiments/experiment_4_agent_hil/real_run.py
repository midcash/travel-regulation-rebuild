"""用真实模型跑一轮：模型会不会自己判断"这里该问人了"？

离线用例全部由脚本 provider 驱动——脚本当然会在写死的位置调用
request_human_input，那证明的是机制，证明不了模型有判断力。
这里换成 DeepSeek，看它在三种场景下**自主**做什么选择：

    A 信息充分  预算 1000，唯一可行组合 473 元      → 不该问，直接 finish
    B 无可行解  预算 400，最便宜可行组合 473 元     → 该问（放宽预算？改需求？）
    C 存在歧义  预算 500，两个组合都可行且各有优劣   → 该问（要哪个？）

判定标准不是"跑通了"，而是**该问的问了、不该问的没问**。
一个见到问题就问的模型和一个从不问的模型，在这个测试里都会露出马脚。

暂停跨越真实进程：`start` 在一个进程里跑到暂停点后退出，`answer` 在另一个
进程里从数据库冷启动继续——这正是实验四要验证的形状，只是把 provider
从脚本换成了真实模型。

    python -m experiments.experiment_4_agent_hil.real_run all
    python -m experiments.experiment_4_agent_hil.real_run one --scenario C
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

if __package__:
    from . import config as C
    from .loop import AgentHilLoop
    from .snapshot import HUMAN_TOOL
    from .store import AgentHilStore
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from experiment_4_agent_hil import config as C
    from experiment_4_agent_hil.loop import AgentHilLoop
    from experiment_4_agent_hil.snapshot import HUMAN_TOOL
    from experiment_4_agent_hil.store import AgentHilStore

ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = Path(__file__).parent / "artifacts"

# 目录里只有这四件：早班车能赶上 08:00 开会，晚班车到 08:40 已经迟到。
# 可行组合只有「早班车 + 任一酒店」，所以预算是唯一变量。
SCENARIOS = {
    "A": {
        "title": "唯一可行解：预算 420 元",
        "budget_cny": 420,
        "expect_human": False,
        "why": ("早班车 73 + 远端酒店 320 = 393 可行；换成附近酒店要 473 就超预算了。"
                "晚班车 08:40 到，赶不上 08:00 的会。所以可行解只有一个，没有可拍板的东西。"),
        "answer": "按你判断的来就行。",
    },
    "B": {
        "title": "无可行解：预算 350 元",
        "budget_cny": 350,
        "expect_human": True,
        "why": "最便宜的可行组合（早班车 + 远端酒店）也要 393 元，现有条件下一个方案都没有，"
               "模型无权自己决定是放宽预算还是改需求。",
        "answer": "预算可以放宽到 1000 元，请按最方便的来。",
    },
    "C": {
        "title": "两个可行解：预算 600 元",
        "budget_cny": 600,
        "expect_human": True,
        "why": ("早班车 + 远端酒店 = 393（便宜但远），早班车 + 附近酒店 = 473（方便但贵），"
                "两者都可行且各有优劣，用户没说过偏好，模型没有客观依据替用户选。"),
        "answer": "选会场附近那家酒店，方便更重要。",
    },
}


def deepseek_config():
    return {"provider": "deepseek",
            "model": os.getenv("DEEPSEEK_MODEL") or "deepseek-v4-flash",
            "base_url": os.getenv("DEEPSEEK_BASE_URL") or "https://api.deepseek.com",
            "tool_mode": "demo"}


def request_for(scenario):
    return {**C.REQUEST, "budget_cny": SCENARIOS[scenario]["budget_cny"]}


def limits():
    return C.limits_dict(max_steps=8, max_tool_calls=8, max_human_requests=3,
                         timeout_s=300.0, human_ttl_minutes=120)


# ---------------- transcript ----------------

def describe_result(message):
    payload = json.loads(message["content"])
    if payload.get("status") == "error":
        return f"错误 {payload['error']['type']}"
    records = payload.get("records", [])
    if not records:
        return "（无记录）"
    return "；".join(
        f"{r['label']} {r['price_cny']}元" + ("" if r.get("meets_deadline", True) else " [赶不上]")
        for r in records)


def transcript(snapshot):
    lines, turn = [], 0
    for message in snapshot["messages"]:
        role = message["role"]
        if role == "assistant":
            turn += 1
            call = message["tool_calls"][0]["function"]
            args = call["arguments"]
            shown = json.dumps(args, ensure_ascii=False)
            lines.append(f"  回合{turn}  模型调用 {call['name']}  {shown[:150]}")
        elif role == "tool":
            name = message.get("tool_name")
            if name == HUMAN_TOOL:
                answer = json.loads(message["content"])
                lines.append(f"          └ 人工回复：{answer.get('text', answer)}")
            else:
                lines.append(f"          └ 结果：{describe_result(message)}")
    return lines


def summarize(store, run_id):
    snapshot = store.load_snapshot(run_id)
    run = snapshot["run"]
    return {
        "run_id": run_id, "status": snapshot["status"],
        "termination": run.get("termination"), "budget_reason": run.get("budget_reason"),
        "human_requests": snapshot["human_requests"],
        "model_calls": run["model_calls"], "tool_calls": run["tool_calls"],
        "elapsed_s": round(snapshot["elapsed_s"], 2),
        "input_tokens": run["input_tokens"], "output_tokens": run["output_tokens"],
        "cost_usd": run.get("cost_usd"), "guardrail_blocks": run["guardrail_blocks"],
        "verdict": snapshot["verdict"],
        "evidence": sorted(snapshot["toolbox_state"]["seen"]),
        "lines": transcript(snapshot),
    }


def questions_asked(store, run_id):
    with store.transaction() as db:
        rows = db.execute("SELECT payload_json FROM human_interactions WHERE run_id=? "
                          "ORDER BY created_at", (run_id,)).fetchall()
    return [json.loads(r["payload_json"]).get("question", "") for r in rows]


# ---------------- 单个阶段 ----------------

def stage_start(db, scenario):
    from travel_agent.settings import load_env
    load_env(ROOT / ".env")
    store = AgentHilStore(db)
    loop = AgentHilLoop(store)
    result = loop.start(C.SCENARIO, deepseek_config(), limits(), request_for(scenario))
    print(json.dumps({"run_id": result["run_id"], "status": result["status"],
                      "step": result["step"],
                      "interaction_id": (result.get("human_request") or {}).get("interaction_id")},
                     ensure_ascii=False))
    return 0


def stage_answer(db, run_id, text):
    from travel_agent.settings import load_env
    load_env(ROOT / ".env")
    store = AgentHilStore(db)
    loop = AgentHilLoop(store)
    snapshot = store.load_snapshot(run_id)
    if snapshot["status"] != "waiting_for_human":
        print(json.dumps({"run_id": run_id, "status": snapshot["status"]}, ensure_ascii=False))
        return 0
    interaction = store.latest_interaction(run_id)
    result = loop.respond(interaction["interaction_id"],
                          {"action": "reply", "text": text})
    print(json.dumps({"run_id": result["run_id"], "status": result["status"],
                      "step": result["step"],
                      "interaction_id": (result.get("human_request") or {}).get("interaction_id")},
                     ensure_ascii=False))
    return 0


def stage_show(db, run_id):
    store = AgentHilStore(db)
    summary = summarize(store, run_id)
    summary["questions"] = questions_asked(store, run_id)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


# ---------------- 驱动一个场景（每步都是独立进程） ----------------

def cli(*args):
    completed = subprocess.run(
        [sys.executable, "-X", "utf8", "-B", "-m",
         "experiments.experiment_4_agent_hil.real_run", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",      # 新增：强制以utf-8读取stdout/stderr
        errors="replace",      # 新增：遇到无法解码字节替换为�，不直接抛异常
        timeout=600
    )
    if completed.returncode != 0:
        raise RuntimeError(f"real_run {' '.join(args)} 退出码 {completed.returncode}\n"
                           f"{completed.stderr[-3000:]}")
    return json.loads(completed.stdout)


def drive(db, scenario, interactive=False, quiet=False):
    spec = SCENARIOS[scenario]
    if not quiet:
        print(f"\n{'=' * 78}\n场景 {scenario}：{spec['title']}\n"
              f"预期：{'需要问人' if spec['expect_human'] else '不需要问人'}"
              f"（{spec['why']}）\n{'=' * 78}")

    state = cli("start", "--db", str(db), "--scenario", scenario)
    run_id = state["run_id"]
    rounds = 0
    while state["status"] == "waiting_for_human" and rounds < 4:
        rounds += 1
        store = AgentHilStore(db)
        question = (questions_asked(store, run_id) or [""])[-1]
        if not quiet:
            print(f"\n  ⏸  进程已退出，模型主动发起了第 {rounds} 次提问：\n     「{question}」")
        answer = spec["answer"]
        if interactive:
            answer = input("     你的回复 > ").strip() or answer
        if not quiet:
            print(f"     → 新进程从数据库恢复，把答复喂回模型：{answer}")
        state = cli("answer", "--db", str(db), "--run", run_id, "--text", answer)

    summary = cli("show", "--db", str(db), "--run", run_id)
    summary["scenario"] = scenario
    summary["expected_human"] = spec["expect_human"]
    summary["asked"] = summary["human_requests"] > 0
    summary["matched_expectation"] = summary["asked"] == spec["expect_human"]

    verdict = summary["verdict"] or {}
    if not quiet:
        print(f"\n  对话回放：")
        for line in summary["lines"]:
            print(line)
        print(f"\n  结果：状态={summary['status']}  终止={summary['termination']}"
              f"  结论={verdict.get('status')}/{verdict.get('reason')}")
        print(f"  计数：模型回合 {summary['model_calls']}，工具调用 {summary['tool_calls']}，"
              f"人工请求 {summary['human_requests']}，活跃耗时 {summary['elapsed_s']}s")
        print(f"  用量：{summary['input_tokens']} in / {summary['output_tokens']} out"
              f"，费用 {summary['cost_usd']}")
        print(f"  证据：{summary['evidence']}")
        flag = "✓ 符合预期" if summary["matched_expectation"] else "✗ 与预期不符"
        print(f"  {flag}：预期{'要问' if spec['expect_human'] else '不问'}，"
              f"实际{'问了' if summary['asked'] else '没问'}")
    return summary


def repeat(scenario, times):
    """同一个场景重复跑，看模型的行为稳不稳定。

    一次运行说明不了问题：模型可能只是这次碰巧问/没问。
    量出"提问率"才能分清是偶发波动还是系统性偏差。
    """
    spec = SCENARIOS[scenario]
    print(f"\n场景 {scenario}：{spec['title']}")
    print(f"预期：{'需要问人' if spec['expect_human'] else '不需要问人'} —— {spec['why']}\n")
    runs = []
    for index in range(1, times + 1):
        db = ARTIFACTS / f"probe-{scenario}-{index}.sqlite3"
        if db.exists():
            db.unlink()
        summary = drive(db, scenario, quiet=True)
        runs.append(summary)
        verdict = summary["verdict"] or {}
        print(f"  第{index}次  提问{summary['human_requests']}次"
              f"  {'问了' if summary['asked'] else '没问'}"
              f"  结论={verdict.get('status')}/{verdict.get('reason')}"
              f"  回合{summary['model_calls']}  耗时{summary['elapsed_s']}s"
              f"  {'✓' if summary['matched_expectation'] else '✗'}")
    asked = sum(1 for r in runs if r["asked"])
    print(f"\n  提问率 {asked}/{times}"
          f"；期望{'要问' if spec['expect_human'] else '不问'}"
          f" → {'判断稳定' if asked in {0, times} and bool(asked) == spec['expect_human'] else '判断有问题'}")
    (ARTIFACTS / f"probe-{scenario}.json").write_text(
        json.dumps(runs, ensure_ascii=False, indent=2), encoding="utf-8")
    return runs


def main(argv=None):
    parser = argparse.ArgumentParser(description="用真实模型验证模型的自主介入判断")
    sub = parser.add_subparsers(dest="command")
    p = sub.add_parser("start"); p.add_argument("--db", required=True)
    p.add_argument("--scenario", required=True, choices=sorted(SCENARIOS)); p.set_defaults(f=stage_start)
    p = sub.add_parser("answer"); p.add_argument("--db", required=True)
    p.add_argument("--run", required=True); p.add_argument("--text", required=True); p.set_defaults(f=stage_answer)
    p = sub.add_parser("show"); p.add_argument("--db", required=True)
    p.add_argument("--run", required=True); p.set_defaults(f=stage_show)
    p = sub.add_parser("one"); p.add_argument("--db"); p.add_argument("--scenario", required=True)
    p.add_argument("--interactive", action="store_true")
    p = sub.add_parser("all"); p.add_argument("--db"); p.add_argument("--interactive", action="store_true")
    p = sub.add_parser("repeat"); p.add_argument("--scenario", required=True, choices=sorted(SCENARIOS))
    p.add_argument("--times", type=int, default=3)

    args = parser.parse_args(argv)
    ARTIFACTS.mkdir(exist_ok=True)
    if args.command == "repeat":
        repeat(args.scenario, args.times)
        return 0
    if args.command in {None, "all"}:
        targets = sorted(SCENARIOS)
    elif args.command == "one":
        targets = [args.scenario]
    else:
        kwargs = {k: v for k, v in vars(args).items() if k in {"scenario", "text"}}
        if getattr(args, "run", None):
            kwargs["run_id"] = args.run          # 命令行用 --run，函数签名用 run_id
        return args.f(args.db, **kwargs)

    results = []
    for scenario in targets:
        db = Path(args.db) if getattr(args, "db", None) else ARTIFACTS / f"real-{scenario}.sqlite3"
        if db.exists():
            db.unlink()
        results.append(drive(db, scenario, interactive=getattr(args, "interactive", False)))

    print(f"\n{'=' * 78}\n汇总\n{'=' * 78}")
    for item in results:
        verdict = item["verdict"] or {}
        print(f"  场景 {item['scenario']}  预期{'要问' if item['expected_human'] else '不问'}"
              f"  实际{'问了' if item['asked'] else '没问'}"
              f"  {'✓' if item['matched_expectation'] else '✗'}"
              f"  结论 {verdict.get('status')}/{verdict.get('reason')}"
              f"  停止原因 {item['termination']}")
    (ARTIFACTS / "real-runs.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n明细已写入 {ARTIFACTS / 'real-runs.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
