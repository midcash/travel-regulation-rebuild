"""暂停快照：跨进程恢复 LLM agent loop 所需的全部状态。

三条设计约束，决定了下面每个字段为什么存在：

1. 自足性——新进程仅凭快照 + provider 工厂 + 工具工厂即可继续执行，
   不依赖原进程的任何内存对象（toolbox 实例、Tracer、闭包变量、局部变量）。
2. 内部格式——messages 只存内部格式（arguments 是 dict，带 tool_name）。
   wire 格式由 provider 适配器在发送时现转（见 providers.py:107-113）。
3. 活跃时长——预算用 elapsed_s 累加器，不用绝对 deadline。
   time.perf_counter() 是进程内单调时钟，基准点跨进程会变，绝对 deadline 直接失效。
   等待人工的时间不计入 elapsed_s：那是"人的耐心"，由 interaction.expires_at 单独管。

暂停点的唯一合法位置：assistant 消息已 append、对应 tool 消息未 append 之间。
OpenAI 风格 API 要求每个 tool_call 后面必须跟一条 tool_call_id 匹配的 tool 消息，
才能进入下一个 assistant 回合（providers.py:132-133 强制校验 id 存在）。
所以"人"扮演的是某一个特定 tool_call_id 的执行者——这是协议约束，不是设计选择。

一个 status、一个 pending_tool_call，就能表达全部暂停/恢复语义：
    RUNNING  + pending=None  → 可以调模型
    RUNNING  + pending≠None  → 上一回合分派没做完（进程死在工具执行前后），重做分派
    WAITING  + pending=人类调用 → 等人工回复
    终态      + pending=None  → 结束（历史里允许留下一个未答复的 finish 调用）
"""
from __future__ import annotations

import json

SNAPSHOT_VERSION = 1

RUNNING = "running"
WAITING = "waiting_for_human"
TERMINAL = {"completed", "cancelled", "expired", "failed"}

HUMAN_TOOL = "request_human_input"
FINISH_TOOL = "finish"

# 快照字段清单。'why' 列是"不持久化会怎样"。
FIELDS = {
    "version":           "int          快照格式版本，未来迁移用",
    "run_id":            "str          主键",
    "trace_id":          "str          新进程段复用同一 trace_id（span 树跨进程会断，见 README）",
    "status":            "str          运行生命周期：running / waiting_for_human / 终态",
    "stage":             "str          model_turn / await_human / done，仅供诊断与界面展示",
    "config":            "dict         重建 provider 与工具集所需：tool_mode / provider / model / scenario_id",
    "scenario":          "dict         工具集构造需要 overrides / faults；也参与 dataset_hash",
    "limits":            "dict         Limits 快照。限额必须在暂停前后是同一份，否则每次恢复都发一份新预算",
    "messages":          "list[dict]   ★主状态。完整对话历史，含 assistant 的 tool_calls 与 tool 结果",
    "step":              "int          下一个待执行的模型回合序号。不从 0 重来，否则 max_steps 失效",
    "pending_tool_call": "dict | None  尚未获得答复的那个 tool_call：{id, name, arguments}",
    "elapsed_s":         "float        ★活跃耗时累加器，替代 agent.py:40-41 的绝对 deadline",
    "human_requests":    "int          已发出的人工请求次数，受 limits.max_human_requests 约束",
    "run":               "dict         agent.py:42-50 那个 run 字典整体（计数器 + 哈希 + pricing）",
    "tool_results":      "dict         validate_verdict 需要；demo 按 name 索引，real 按 call_id",
    "toolbox_state":     "dict         ★工具集的可变状态，裸 dict：{calls, faults, seen}",
    "verdict":           "dict | None  中断时保留的上一版结论，终态时是最终结论",
}


class SnapshotError(Exception):
    """快照自检失败：恢复时不变量已被破坏。"""


def new_snapshot(*, run_id, trace_id, config, scenario, limits, prompt, request, run):
    """建 run 时的初始快照。messages 前两条的拼法与 agent.py:52-53 一致。"""
    return {
        "version": SNAPSHOT_VERSION,
        "run_id": run_id,
        "trace_id": trace_id,
        "status": RUNNING,
        "stage": "model_turn",
        "config": config,
        "scenario": scenario,
        "limits": limits,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps(request, ensure_ascii=False)},
        ],
        "step": 0,
        "pending_tool_call": None,
        "elapsed_s": 0.0,
        "human_requests": 0,
        "run": run,
        "tool_results": {},
        "toolbox_state": {"calls": 0, "faults": dict(scenario.get("faults", {})), "seen": {}},
        "verdict": None,
    }


def encode(snapshot):
    """落库用。sort_keys 让同一状态有唯一文本表示，便于比对与排查。"""
    return json.dumps(snapshot, ensure_ascii=False, sort_keys=True)


def decode(text):
    snapshot = json.loads(text)
    check_snapshot(snapshot)
    return snapshot


# ---------------------------------------------------------------------------
# 恢复时立刻自检。这几条断言就是实验要抓的那几类 bug 的探测器：
#   step/model_calls 分叉 → 暂停恢复把回合算重了（用例 3）
#   重复 tool_call id / 两条同 id 的 tool 回复 → 恢复时重复 append（用例 5）
#   配额被重置 → 跨暂停没有累计（用例 6）
# ---------------------------------------------------------------------------

def check_snapshot(snapshot):
    if snapshot.get("version") != SNAPSHOT_VERSION:
        raise SnapshotError(f"unsupported snapshot version: {snapshot.get('version')}")
    missing = [key for key in FIELDS if key not in snapshot]
    if missing:
        raise SnapshotError(f"missing fields: {missing}")
    if snapshot["status"] not in TERMINAL | {RUNNING, WAITING}:
        raise SnapshotError(f"invalid status: {snapshot['status']}")
    if snapshot["step"] != snapshot["run"]["model_calls"]:
        raise SnapshotError(
            f"step {snapshot['step']} != model_calls {snapshot['run']['model_calls']}")
    if snapshot["elapsed_s"] < 0:
        raise SnapshotError("negative elapsed time")
    for key in ("max_steps", "max_tool_calls", "max_human_requests", "timeout_s"):
        if key not in snapshot["limits"]:
            raise SnapshotError(f"limits missing {key}")
    if snapshot["human_requests"] > snapshot["limits"]["max_human_requests"]:
        raise SnapshotError("human request limit already exceeded")
    check_messages(snapshot)


def check_messages(snapshot):
    """对话结构必须时刻是一个"合法前缀"。这条抓的是 messages 重复 append / 漏 append。

    终态下允许留下一个未答复的 tool_call：finish 调用本身不需要工具回复，
    而且 run 已经结束，messages 不会再发给模型（对比 agent.py:104-113 从不回复 finish）。
    """
    messages = snapshot["messages"]
    if len(messages) < 2 or messages[0].get("role") != "system" or messages[1].get("role") != "user":
        raise SnapshotError("conversation must start with system + user")
    # 回复必须一条对一条，且不能重复。用 set 会漏掉"同一个 id 回复了两遍"，
    # 而那正是幂等失效（用例 5）唯一的表现形式——必须按出现次数查。
    replies = [m.get("tool_call_id") for m in messages
               if m.get("role") == "tool" and m.get("tool_call_id")]
    if len(set(replies)) != len(replies):
        raise SnapshotError(f"duplicate tool reply for the same tool_call id: {sorted(replies)}")
    answered = set(replies)
    asked = []
    for message in messages:
        if message.get("role") != "assistant":
            continue
        calls = message.get("tool_calls") or []
        # 实验约束：一个模型回合只有一个工具调用（与 agent.py:91-92 一致）。
        if len(calls) != 1:
            raise SnapshotError("exactly one tool call per assistant turn required")
        # 落库前一律补齐 id（见 ensure_ids），所以历史里不允许再出现无 id 的调用：
        # 没有 id 就没法把回复和调用对上，重复喂回也就无从检测。
        if not calls[0].get("id"):
            raise SnapshotError("assistant tool call without an id")
        asked.append(calls[0]["id"])
    if len(set(asked)) != len(asked):
        raise SnapshotError("duplicate tool_call id in history")
    orphans = [x for x in asked if x not in answered]
    pending = snapshot["pending_tool_call"]
    if len(orphans) > 1:
        raise SnapshotError(f"more than one unanswered tool call: {orphans}")
    if pending is None:
        if orphans and snapshot["status"] not in TERMINAL:
            raise SnapshotError(f"unanswered tool call {orphans} outside a terminal run")
    else:
        if orphans != [pending["id"]]:
            raise SnapshotError(f"unanswered tool call {orphans} != pending [{pending['id']}]")
        if snapshot["status"] == WAITING and pending["name"] != HUMAN_TOOL:
            raise SnapshotError(f"waiting for human on tool {pending['name']}")
    if snapshot["status"] == WAITING and pending is None:
        raise SnapshotError("waiting for human without a pending tool call")


def remaining_s(snapshot):
    """剩余活跃预算。

    这是整个实验最容易被忽略的一处：恢复时 perf_counter 的基准点已经变了，
    但 elapsed_s 是暂停前落库的值——所以它必须持久化，且只有它是可信的。
    """
    return snapshot["limits"]["timeout_s"] - snapshot["elapsed_s"]


def ensure_ids(message, step):
    """给 assistant 消息里没有 id 的 tool_call 补一个稳定的合成 id。

    没有 id 就无法把回复和调用对上，重复喂回也就检测不出来。
    Baseline 的 tool_call()（providers.py:14-16）不生成 id，真实 provider 都生成
    （providers.py:132-133 强制校验），所以合成只对不关心 id 的 provider 发生。
    代价：整段对话必须始终发给同一个 provider——config 已随快照持久化，这一点成立。
    """
    for call in message.get("tool_calls") or []:
        if not call.get("id"):
            call["id"] = f"synthetic:{step}"
    return message


def human_tool_message(pending, response):
    """把人工回复包装成一条 tool 消息喂回模型（对应 agent.py:155-159）。

    tool_call_id 必须与 assistant 那条 request_human_input 的调用 id 一致，
    否则 OpenAI 风格 API 会直接判定格式错误（providers.py:132-133 已经在
    assistant 方向强制过这件事，这里是回复方向的同一约束）。
    """
    return {"role": "tool", "tool_name": HUMAN_TOOL, "tool_call_id": pending["id"],
            "content": json.dumps(response, ensure_ascii=False)}


def tool_message(pending, result):
    """普通工具的回复消息，格式与 agent.py:155-159 一致。"""
    return {"role": "tool", "tool_name": pending["name"], "tool_call_id": pending["id"],
            "content": json.dumps(result, ensure_ascii=False)}


def public(snapshot, human_request=None):
    """给界面/CLI 的投影。不返回 messages（可能很长，且含模型原始输出）。"""
    result = {
        "run_id": snapshot["run_id"], "status": snapshot["status"], "stage": snapshot["stage"],
        "step": snapshot["step"], "elapsed_s": round(snapshot["elapsed_s"], 3),
        # 把剩余预算显式暴露出来，是为了让它可断言：否则"预算有没有在恢复时被
        # 重新发一份"只能靠计时赛跑去猜，而脚本 provider 的调用只要微秒级，猜不出来。
        "remaining_s": round(remaining_s(snapshot), 3),
        "human_requests": snapshot["human_requests"],
        "tool_calls": snapshot["run"]["tool_calls"], "model_calls": snapshot["run"]["model_calls"],
        "verdict": snapshot["verdict"],
    }
    if human_request is not None:
        result["human_request"] = human_request
    return result
