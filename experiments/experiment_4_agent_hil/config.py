"""实验四的限额、默认场景与脚本 provider 的常用脚本。"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from .snapshot import FINISH_TOOL, HUMAN_TOOL
from .tools import GET_PRICE


@dataclass(frozen=True)
class Limits:
    """比 agent.py:14-21 多两项，都直接服务于暂停恢复：

    max_human_requests —— 模型"问不停"时的独立上限。只靠 max_steps 兜底也能停，
        但终止原因会退化成 budget_exceeded: steps，说不清是模型不收敛还是任务太复杂。
    human_ttl_minutes  —— 人工等待的有效期，接管 runtime.py:99 的 ttl_minutes。
        它约束的是"人的耐心"，与 timeout_s（活跃预算）是两回事，不能混用。
    """
    max_steps: int = 10
    max_tool_calls: int = 12
    max_human_requests: int = 3
    timeout_s: float = 180.0
    model_timeout_s: float = 45.0
    max_tokens: int = 24000
    human_ttl_minutes: int = 60


def limits_dict(**overrides):
    return {**asdict(Limits()), **overrides}


SCENARIO = {"id": "exp4-demo", "faults": {}}

REQUEST = {"origin": "杭州", "destination": "上海",
           "budget_cny": 1000, "meeting_at": "2026-09-12T08:00+08:00"}


def config(script, *, provider="scripted", model="scripted-v1", **extra):
    """config 是快照里唯一用于重建 provider 的东西，所以脚本本身也在这里——
    恢复后的进程跑的是同一份脚本，跨进程测试才谈得上"确定性"。"""
    return {"provider": provider, "model": model, "tool_mode": "demo",
            "script": list(script), **extra}


def price(item_id):
    return {"name": GET_PRICE, "arguments": {"item_id": item_id}}


def ask(question="请确认是否接受这个组合？", kind="confirm", options=None):
    return {"name": HUMAN_TOOL,
            "arguments": {"question": question, "kind": kind, "options": options or []}}


def tell(status="planned", reason="feasible", evidence_ids=()):
    return {"name": FINISH_TOOL,
            "arguments": {"status": status, "reason": reason,
                          "evidence_ids": list(evidence_ids)}}


# 给 CLI 和"看得见"的演示用。测试自己写脚本，不依赖这些名字。
SCRIPTS = {
    # 查一次 → 问一次 → 再查一次 → 收尾。覆盖"暂停夹在两个工具调用之间"。
    "happy": [price("train_early"), ask("是否接受早班高铁 + 附近酒店？"),
              price("hotel_near"), tell(evidence_ids=["train_early", "hotel_near"])],
    # 一次运行里暂停两次，中间还有一次真实工具调用。
    "two-asks": [price("train_early"),
                 ask("交通选早班还是晚班？", kind="choose", options=["早班高铁", "晚班高铁"]),
                 price("hotel_far"),
                 ask("酒店接受远端的吗？", options=["接受", "不接受"]),
                 tell(evidence_ids=["train_early", "hotel_far"])],
    # 模型问不停：条数超过 max_human_requests，用来验证配额跨暂停累计。
    "ask-forever": [ask(f"第 {n} 次确认：可以继续吗？") for n in range(1, 9)],
    # finish 引用没见过的证据 id：走 guardrail 分支 → invalid_response。
    "unknown-evidence": [price("train_early"),
                         tell(evidence_ids=["train_early", "never_seen"])],
    # 一次查询也不做就下结论：同样被校验拦住。
    "no-evidence": [tell(status="unavailable", reason="no_feasible_route")],
}
