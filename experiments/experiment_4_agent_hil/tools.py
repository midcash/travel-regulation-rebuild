"""实验四的 demo 工具集：可变状态完全外部化，实例本身不持有任何状态。

契约（loop.py 依赖它，也是"跨进程恢复"能成立的关键）：

    DemoTools(config, scenario, state)
        state 就是 snapshot["toolbox_state"] 那个裸 dict 本身，不是副本。
        工具在 execute 里对 state 的修改会直接反映到快照上，
        下一次 save_snapshot 就落库——所以工具不需要 snapshot()/restore() 方法，
        state dict 本身就是序列化格式。

    state 形状：{"calls": int, "faults": {工具名: 剩余故障次数}, "seen": {证据 id: 记录}}

新进程重建工具集时，把落库的 state 原样传进来，计数器和证据字典就回到了
暂停前那一刻。这正是 real_tools.py:21-29 里那堆实例字段（calls/http_requests/
seen/history/_transport_attempts）没法直接做到的事。

工具集保持线性：一次调用一定产出一条结果，不做内部重试。
agent.py:115-153 那个重试循环故意没有搬过来——本实验要验证的是暂停恢复机制，
每多一个分支，出问题时就多一分"是机制的 bug 还是业务的 bug"的纠缠。
超时以 error 结果的形式返回（对应 agent.py:150 的最终尝试分支）。
"""
from __future__ import annotations

from .snapshot import FINISH_TOOL, HUMAN_TOOL

GET_PRICE = "get_price"

# 固定的候选项目录。价格与时间只为教学演示，不是真实数据。
CATALOG = {
    "train_early": {"id": "train_early", "kind": "transport", "label": "早班高铁 06:40-07:50",
                    "price_cny": 73, "arrive": "07:50"},
    "train_late": {"id": "train_late", "kind": "transport", "label": "晚班高铁 06:20-08:40",
                   "price_cny": 110, "arrive": "08:40"},
    "hotel_near": {"id": "hotel_near", "kind": "stay", "label": "会场附近酒店",
                   "price_cny": 400},
    "hotel_far": {"id": "hotel_far", "kind": "stay", "label": "远端商务酒店",
                  "price_cny": 320},
}

# 实验的判定标准是"暂停恢复机制对不对"，不是业务逻辑对不对，
# 所以业务校验只保留一条：引用的证据必须真的见过。
VALID_STATUS = ("planned", "unavailable", "needs_review")
VALID_REASON = ("feasible", "no_feasible_route", "missing_data")


def function(name, description, properties, required=()):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties,
                       "required": list(required), "additionalProperties": False}}}


SCHEMAS = [
    function(GET_PRICE, "查询一个候选项的价格、到达时间和是否满足最晚到场要求。返回的记录是唯一可引用的证据。",
             {"item_id": {"type": "string", "enum": sorted(CATALOG)}}, ["item_id"]),
    function(HUMAN_TOOL,
             "缺少必要信息、需要在候选项之间取舍、或需要用户确认时调用。这是获得用户答复的唯一方式；"
             "调用后本次运行会暂停并等待真人回复，回复会作为本工具的返回结果交还给你。",
             {"question": {"type": "string", "description": "要问用户的问题，中文"},
              "kind": {"type": "string", "enum": ["choose", "confirm"]},
              "options": {"type": "array", "items": {"type": "string"},
                          "description": "建议选项，可为空"}},
             ["question", "kind"]),
    function(FINISH_TOOL, "提交最终结论。evidence_ids 只能引用工具已经返回过的 id。",
             {"status": {"type": "string", "enum": list(VALID_STATUS)},
              "reason": {"type": "string", "enum": list(VALID_REASON)},
              "evidence_ids": {"type": "array", "items": {"type": "string"}}},
             ["status", "reason", "evidence_ids"]),
]

TOOL_NAMES = tuple(schema["function"]["name"] for schema in SCHEMAS)

PROMPT = """你是一个差旅助手。用工具查询候选项，然后给出结论。

PROCEDURE:
1. 用 get_price 查询你需要的候选项。每次只查一个。
2. 判断这一步要不要交给用户拍板：
   - 只有一个可行选项 → 直接 finish，不要为了确认而提问。
   - 有两个及以上都可行、且你没有客观依据取舍的选项 → 调用 request_human_input 让用户选。
   - 缺少完成任务所必需的信息（例如现有条件下一个可行方案都没有）→ 调用 request_human_input 询问。
   request_human_input 是获得用户答复的唯一方式：调用后运行会暂停，
   用户答复会作为该工具的返回结果交给你。
   用户没说过的偏好不要替他猜；但也不要在没有歧义时打扰用户。
3. 信息齐备后调用 finish。evidence_ids 只能引用 get_price 已经返回过的 id，不要编造。

每个回合只能调用一个工具。不要重复查询同一个 id。
"""


def new_state(scenario):
    """工具集状态的初始值。faults 从场景带过来，是演示"跨暂停累计"最直观的一处。"""
    return {"calls": 0, "faults": dict(scenario.get("faults", {})), "seen": {}}


class DemoTools:
    """无状态工具集。全部可变状态在 self.state 这个裸 dict 里。"""

    def __init__(self, config, scenario, state):
        self.config = config
        self.scenario = scenario
        self.state = state

    def execute(self, name, args):
        self.state["calls"] += 1
        call_id = f"call:{self.state['calls']}"
        if name == GET_PRICE:
            return self._get_price(call_id, args)
        raise ValueError("Unknown tool")

    def _get_price(self, call_id, args):
        if not isinstance(args, dict) or set(args) != {"item_id"}:
            raise ValueError("Unknown tool or unexpected arguments")
        item_id = args["item_id"]
        if item_id not in CATALOG:
            raise ValueError("Unknown item")
        # 故障注入：每次消费一次，且消费结果随 state 一起落库，
        # 所以"暂停前用过一次故障、恢复后不再触发"是可以被验证的。
        if self.state["faults"].get(GET_PRICE, 0) > 0:
            self.state["faults"][GET_PRICE] -= 1
            return {"call_id": call_id, "tool": GET_PRICE, "status": "error",
                    "error": {"type": "TimeoutError", "message": "模拟后端超时", "retryable": True},
                    "records": [], "evidence_refs": []}
        record = dict(CATALOG[item_id])
        record["meets_deadline"] = record.get("arrive", "07:50") <= "08:00"
        self.state["seen"][item_id] = record
        return {"call_id": call_id, "tool": GET_PRICE, "status": "ok",
                "records": [record], "evidence_refs": [item_id]}
