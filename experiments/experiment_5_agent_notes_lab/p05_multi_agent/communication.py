"""Agent 之间的通信与状态管理。对应笔记第 6.4 节。

笔记把通信拆成三个维度，这个文件三个都实现了：

  1. **模式**（控制权在哪）
       移交 Handoff        控制权转移，交棒的 Agent 自己退出
       工具调用 ToolCall   主管保留控制权，等结果回来自己决定下一步
       消息协作            多个长期驻留 Agent 持续交换消息，可横向通信

  2. **传什么**（上下文规模）
       共享完整推理数据    协作更强，但状态空间迅速膨胀
       只共享最终结果      上下文可控，但要为每个 Agent 定义清晰的结果模式

  3. **介质**（怎么传）
       内存通道 in-memory  子 Agent 输出作为 tool_result 同步返回主上下文
       文件邮箱 file mailbox 写进对方 inbox 文件，接收方下一轮读取并注入

第二点是最容易被忽略、也最容易出事的：**默认共享全部内部状态是个陷阱**。
笔记的措辞是"由外部框架构造新的上下文，只传递必要的消息、工具参数和任务产物"。
main.py 里把两种做法的 token 开销并排量了出来。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from common.llm import Message, count_message_tokens
from common.tools import ToolRegistry

MAILBOX = Path(__file__).resolve().parent / "mailbox"


@dataclass
class AgentContext:
    """一个 Agent 能看到的东西。

    注意这是**外部框架构造出来的**，不是某个 Agent 的内部状态直接漏给另一个。
    多 Agent 系统里"谁能看到什么"必须是显式决定的。
    """

    name: str
    system_prompt: str
    messages: list[Message] = field(default_factory=list)
    artifacts: dict[str, Any] = field(default_factory=dict)

    def tokens(self) -> int:
        return count_message_tokens(
            [{"role": "system", "content": self.system_prompt}] + self.messages
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "agent": self.name,
            "messages": list(self.messages),
            "artifacts": dict(self.artifacts),
        }


@dataclass
class HandoffResult:
    """移交之后，新 Agent 拿到的东西。"""

    to_agent: str
    context: AgentContext
    carried_messages: int
    note: str


def handoff(from_agent: AgentContext, to_agent: str, system_prompt: str) -> HandoffResult:
    """移交：控制权完整转移，交棒方退出。

    笔记的比喻是"接力跑把棒交出去"。关键在于**带走了多少历史**：
    移交通常要把上下文一起交出去，否则接棒的 Agent 得从头问一遍。
    """
    new_context = AgentContext(
        name=to_agent,
        system_prompt=system_prompt,
        messages=list(from_agent.messages),  # 上下文整体转移
        artifacts=dict(from_agent.artifacts),
    )
    return HandoffResult(
        to_agent=to_agent,
        context=new_context,
        carried_messages=len(new_context.messages),
        note="控制权已转移，原 Agent 退出",
    )


@dataclass
class DelegationResult:
    """委派：主管保留控制权，子 Agent 的结果作为 tool_result 回到主管上下文。"""

    sub_agent: str
    tool_result: Message
    sub_context_tokens: int
    returned_tokens: int
    note: str


def delegate(
    orchestrator: AgentContext,
    sub_agent: str,
    system_prompt: str,
    task: str,
    sub_messages: list[Message],
    final_result: str,
    call_id: str = "call_sub_1",
) -> DelegationResult:
    """委派：主管把任务当工具调出去，只把**最终结果**收回来。

    这是 Claude Code 里 SubAgent 的做法，也是控制上下文规模最有效的一招：
    子 Agent 在私有空间里折腾多久都行，回到主管上下文的只有一行结果。
    """
    sub_context = AgentContext(name=sub_agent, system_prompt=system_prompt, messages=sub_messages)
    tool_result: Message = {
        "role": "tool",
        "tool_call_id": call_id,
        "name": f"subagent:{sub_agent}",
        "content": final_result,
    }
    orchestrator.messages.append(tool_result)
    return DelegationResult(
        sub_agent=sub_agent,
        tool_result=tool_result,
        sub_context_tokens=sub_context.tokens(),
        returned_tokens=count_message_tokens([tool_result]),
        note="子 Agent 的中间过程留在它自己的上下文里，只有最终结果回到主管",
    )


class FileMailbox:
    """文件邮箱：成员把消息写进对方 inbox 文件，接收方下一轮读取并注入上下文。

    这是 Claude Code 里 AgentTeam 的做法。相比内存通道，它天然支持
    "接收方现在不在线"——消息落盘等着，下次启动再读。
    """

    def __init__(self, root: Path | None = None):
        self.root = Path(root or MAILBOX)
        self.root.mkdir(parents=True, exist_ok=True)

    def _inbox(self, agent: str) -> Path:
        return self.root / f"{agent}.inbox.jsonl"

    def send(self, to_agent: str, sender: str, topic: str, body: str) -> Path:
        path = self._inbox(to_agent)
        record = {"from": sender, "topic": topic, "body": body}
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return path

    def receive(self, agent: str) -> list[dict[str, Any]]:
        path = self._inbox(agent)
        if not path.exists():
            return []
        records = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
        return records

    def reset(self) -> None:
        for path in self.root.glob("*.inbox.jsonl"):
            path.unlink()


def collaborative_round(
    agents: dict[str, AgentContext],
    mailbox: FileMailbox,
    round_index: int,
) -> list[dict[str, Any]]:
    """一轮消息协作：每个 Agent 读自己的 inbox，处理后回一条。

    消息协作和上面两种的本质区别：Agent 是**长期驻留**的，
    不是"叫一次就退出"。所以它允许横向通信和并行协作，
    代价是通信量按 O(N^2) 涨 —— 通常要配通信限流/白名单。
    """
    delivered: list[dict[str, Any]] = []
    names = list(agents)
    for name in names:
        inbox = mailbox.receive(name)
        if inbox:
            delivered.append({"agent": name, "received": len(inbox)})
        # 每个 Agent 向其他所有人广播自己这一轮的结论
        for peer in names:
            if peer == name:
                continue
            mailbox.send(
                peer,
                sender=name,
                topic=f"round-{round_index}",
                body=f"{name} 第 {round_index} 轮结论：价格已确认，政策待核对",
            )
    return delivered


def subagent_as_tool(name: str, description: str, run: Any) -> Any:
    """把一个子 Agent 包装成工具——这是"委派"模式最常见的落地形式。"""
    from common.tools import Tool

    def handler(task: str) -> dict[str, Any]:
        result = run(task)
        return {"sub_agent": name, "task": task, "result": result}

    return Tool(
        name=f"ask_{name}",
        description=description,
        parameters={
            "type": "object",
            "properties": {"task": {"type": "string", "description": "交给该子 Agent 的任务"}},
            "required": ["task"],
        },
        handler=handler,
        annotations={"read_only": True, "idempotent": True},
    )


def registry_with_subagents(*tools: Any) -> ToolRegistry:
    registry = ToolRegistry("p05")
    for tool in tools:
        registry.register(tool)
    return registry
