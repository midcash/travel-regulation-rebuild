"""Numbered terminal interaction; runtime checkpoints are saved before input()."""
from datetime import datetime
import sqlite3
import uuid

if __package__:
    from .runtime import HilError, TERMINAL
else:
    from runtime import HilError, TERMINAL


STATUS = {
    "running": "执行中", "waiting_for_human": "等待你的选择",
    "completed": "已完成", "cancelled": "已取消", "expired": "已过期",
    "failed": "执行失败",
}


def choose(prompt, count):
    while True:
        value = input(prompt).strip().lower()
        if value in {"q", "0"}:
            return None
        if value.isascii() and value.isdigit() and 1 <= int(value) <= count:
            return int(value) - 1
        print(f"请输入 1–{count}，或输入 0 返回。")


def show_plan(plan):
    print("\n── 方案摘要 ──")
    for item in plan["details"]:
        print(f"  {item['label']}  ·  {item['price_cny']} 元")
    print(f"总费用：{plan['total_cny']} 元")
    print(f"预计到场：{plan['arrival_at'][11:16]}；最晚到场：{plan['deadline'][11:16]}")
    print("校验：通过" if plan["status"] == "planned" else "校验：未通过，超出预算或到场过晚，不能确认。")


def select_item(options, title):
    print(f"\n{title}")
    for i, item in enumerate(options, 1):
        timing = (f"{item['start_at'][11:16]} 出发 / {item['end_at'][11:16]} 到站"
                  if item["kind"] == "transport"
                  else f"预计 {item['arrival_at'][11:16]} 到场")
        print(f"  {i}. {item['label']}  ·  {item['price_cny']} 元  ·  {timing}")
    index = choose("输入编号（0 保存并返回主菜单）：", len(options))
    return None if index is None else options[index]["id"]


def interact(loop, result):
    while result["status"] not in TERMINAL:
        print("\n── 已保存进度，等待人工决策 ──")
        request = result["human_request"]
        print("本次确认有效至：" + datetime.fromisoformat(request["expires_at"]).astimezone().strftime("%m-%d %H:%M"))
        payload = {}
        if request["kind"] == "select_candidate":
            print("  1. 选择交通和酒店    2. 取消任务    0. 保存并返回")
            choice = choose("请选择：", 2)
            if choice is None:
                return
            action = "cancel"
            if choice == 0:
                options = request["payload"]["options"]
                transport = select_item([x for x in options if x["kind"] == "transport"], "选择交通")
                if transport is None:
                    return
                stay = select_item([x for x in options if x["kind"] == "stay"], "选择酒店")
                if stay is None:
                    return
                action, payload = "select", {"transport_id": transport, "stay_id": stay}
        else:
            show_plan(result["plan"])
            print("  1. 确认方案    2. 修改预算并重新选择    3. 重新选择候选\n  4. 取消任务    0. 保存并返回")
            choice = choose("请选择：", 4)
            if choice is None:
                return
            if choice == 0 and result["plan"]["status"] != "planned":
                print("当前方案未通过校验，请重新选择候选或修改预算。")
                continue
            action = ["approve", "revise", "revise", "cancel"][choice]
            if choice == 1:
                while True:
                    value = input("新预算（整数元；q 返回）：").strip()
                    if value.lower() == "q":
                        return
                    if value.isascii() and value.isdigit():
                        payload = {"request_patch": {"budget_cny": int(value)}}
                        break
                    print("请输入非负整数。")
        if action == "cancel" and input("确定取消？输入 y 确认：").strip().lower() != "y":
            continue
        try:
            result = loop.respond(request["interaction_id"], action, payload,
                                  response_id=uuid.uuid4().hex,
                                  expected_state_version=request["state_version"])
        except HilError:
            print("本次操作未接受，正在重新读取任务状态；可能已被其他窗口处理。")
            result = loop.resume(result["run_id"])
    if result["status"] == "completed":
        show_plan(result["plan"])
        print("\n方案已确认，任务完成。此实验不会预订或付款。")
    else:
        print("\n任务" + STATUS.get(result["status"], result["status"]) + "。")
        if result["status"] == "expired":
            print("等待已超过有效期，请在主菜单开始新任务。")


def run_menu(loop):
    print("\n╭──────────────────────────────────╮\n│  差旅助手 · 人工介入学习实验      │\n╰──────────────────────────────────╯")
    print("离线固定案例：杭州 → 上海，2026-09-12 08:00 东方明珠塔开会。")
    print("输入编号即可操作；0 返回，Ctrl+C 保存退出。未提交的选择不会保存。")
    try:
        while True:
            print("\n  1. 开始新任务\n  2. 继续已有任务\n  3. 查看运行记录\n  0. 退出")
            choice = choose("你 > ", 3)
            if choice is None:
                return 0
            try:
                if choice == 0:
                    print("助手 > 正在查询模拟候选……")
                    interact(loop, loop.start())
                    continue
                runs = loop.store.list_runs()
                if choice == 1:
                    runs = [x for x in runs if x["status"] not in TERMINAL]
                if not runs:
                    print("暂无可选任务。")
                    continue
                for i, row in enumerate(runs, 1):
                    print(f"  {i}. {row['created_at'][:19]} UTC · {STATUS.get(row['status'], row['status'])} · {row['run_id'][:8]}")
                index = choose("选择任务编号（0 返回）：", len(runs))
                if index is None:
                    continue
                run_id = runs[index]["run_id"]
                if choice == 1:
                    interact(loop, loop.resume(run_id))
                else:
                    state = loop.store.get_run(run_id)
                    print(f"状态：{STATUS.get(state['status'], state['status'])}；需求版本：{state['request_version']}")
                    if state["plan"]:
                        show_plan(state["plan"])
                    for entry in loop.store.list_events(run_id):
                        print(f"  {entry['created_at'][:19]} · {entry['type']}")
            except (HilError, sqlite3.Error) as exc:
                print(f"操作未完成：{exc}\n可返回菜单重新读取任务；运行进度保存在本地数据库。")
    except (EOFError, KeyboardInterrupt):
        print("\n已退出。已提交的进度已保存，下次选择“继续已有任务”。")
        return 0
