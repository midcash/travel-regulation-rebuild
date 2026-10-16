"""provider 工厂。

脚本 provider 有一个刻意的设计：**它是无状态的，第几个回合由对话历史推导**。

原因：loop.py 的 _turn() 每次都用 provider_factory(config) 现造一个 provider
（快照里不存实例，只存 config）。如果 ScriptedProvider 把进度记在实例里，
那么每次恢复、甚至每个回合都会从脚本第 0 条重新开始——这不是脚本 provider 的
小毛病，而是"哪些状态必须持久化"的一个真实案例。改从 messages 里数 assistant
消息条数，脚本进度就天然跟着快照走，跨进程也不会错位。

这与 Baseline.chat（providers.py:25-27 从 messages 重建观测）是同一种思路。
"""
from __future__ import annotations

from travel_agent.providers import make_provider


class ScriptExhausted(Exception):
    """脚本用完了。用来确定性地构造"模型问不停"这类场景。"""


class ScriptedProvider:
    """按脚本吐工具调用，不联网、可复现。

    script 是 [{"name": ..., "arguments": {...}}, ...]，会被存进快照的 config 里，
    所以恢复后的进程跑的是同一份脚本。
    """

    name = "scripted"

    def __init__(self, script, model="scripted-v1"):
        self.script = list(script)
        self.model = model

    def chat(self, messages, schemas, timeout):
        turn = sum(1 for m in messages if m.get("role") == "assistant")
        if turn >= len(self.script):
            raise ScriptExhausted(f"script has no entry for turn {turn}")
        spec = self.script[turn]
        message = {"role": "assistant", "content": "", "tool_calls": [{
            "id": f"scripted:{turn}", "type": "function",
            "function": {"name": spec["name"], "arguments": spec.get("arguments", {})}}]}
        return {"message": message, "input_tokens": 10, "output_tokens": 5,
                "cache_hit_tokens": 0, "cost_usd": None}


def build_provider(config):
    if config["provider"] == "scripted":
        return ScriptedProvider(config.get("script", []), model=config.get("model", "scripted-v1"))
    return make_provider(config["provider"], model=config.get("model"),
                         base_url=config.get("base_url"))
