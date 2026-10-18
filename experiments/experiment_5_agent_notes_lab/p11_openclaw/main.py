"""原型 11：OpenClaw 的事件驱动架构。

运行：python experiments/experiment_5_agent_notes_lab/p11_openclaw/main.py
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from common import trace  # noqa: E402
from openclaw_runtime import (  # noqa: E402
    BootstrapFiles,
    Gateway,
    OpenClawAgent,
    OpenClawSystem,
    RetrievalIndex,
    SecureToolRunner,
    SessionTranscript,
    TriggerLayer,
)


def build_system() -> tuple[TriggerLayer, OpenClawSystem, Gateway, SessionTranscript]:
    triggers = TriggerLayer()
    transcript = SessionTranscript()
    index = RetrievalIndex(
        {
            "MEMORY.md": "用户偏好：回答要简洁，项目使用 Python。",
            "memory/2026-09-11.md": "昨天决定：OpenClaw 的 memory_search 采用按需检索。",
        }
    )
    tools = SecureToolRunner(index)

    def policy(event, history):
        text = str(event.payload.get("text", ""))
        if "偏好" in text or "昨天" in text:
            return "memory_search", {"query": "用户偏好 OpenClaw"}
        if event.kind == "heartbeat":
            return "心跳检查完成：没有需要主动处理的事项。"
        if event.kind == "cron":
            return "定时任务完成：生成了日报摘要。"
        return f"Agent 已处理 {event.channel} 的消息：{text}"

    agent = OpenClawAgent(
        BootstrapFiles("一个本地个人助手", "只使用允许的工具；高风险动作必须人工确认"),
        transcript,
        tools,
        policy,
    )
    gateway = Gateway({"local-token"})
    return triggers, OpenClawSystem(agent, gateway), gateway, transcript


def main() -> None:
    trace.section("原型 11：OpenClaw 事件驱动 Agent", "第 11 章：触发层 -> Gateway -> Agent -> 持久化")
    triggers, system, gateway, transcript = build_system()
    events = [
        triggers.emit("message", "s1", {"text": "你好"}, "feishu"),
        triggers.emit("heartbeat", "s1", {"text": "周期自检"}, "scheduler"),
        triggers.emit("cron", "s1", {"text": "生成日报"}, "scheduler"),
        triggers.emit("hook", "s1", {"text": "session_started"}, "gateway"),
        triggers.emit("webhook", "s1", {"text": "GitHub 有新告警"}, "github"),
    ]
    trace.note("五种触发源都只是事件；真正的推理发生在 Agent 层。")
    for event in events:
        result = system.dispatch(event, "local-token")
        trace.kv(f"{event.kind:9} -> {event.channel}", result["answer"])

    trace.section("Gateway 的职责边界")
    trace.kv("路由审计", gateway.audit)
    trace.kv("Gateway 是否调用 LLM", "否：只认证、归一化、投递")
    trace.kv("Agent 工具调用数", system.agent.tool_calls)

    trace.section("按需记忆与安全护栏")
    memory_event = triggers.emit("message", "s1", {"text": "昨天关于偏好的决定是什么？"}, "feishu")
    trace.kv("memory_search", system.dispatch(memory_event, "local-token"))
    trace.kv("未授权工具", system.agent.tools.call("exec_shell", {"command": "rm -rf /"}))
    trace.kv("会话条目数", len(transcript.entries))
    trace.note("只有写入记忆文件的内容才能被 RetrievalIndex 找到；聊天本身不会自动变成长期记忆。")


if __name__ == "__main__":
    main()
