"""A2A 客户端：发现、派发、跟进、收产物。

这个文件和 p06 的 `client.py` 放在一起看，差别最清楚：

    p06 的 MCPClient                    这个 A2AClient
    ---------------------------------   ------------------------------------
    initialize()  握手                   discover(url)  拉卡片
    list_tools()  列工具                 route(request) 按技能找人 ← 多出来的一步
    call_tool()   调用，**同步返回结果**   send_task()    派发，**返回 task_id**
    （没有）                              wait()         跟进，直到有结果或需要输入
    （没有）                              send_message() 补充输入，任务接着跑
    （没有）                              cancel()       取消一个正在跑的任务

右边那三个「没有」对应的是 A2A 的核心：
**委派出去的工作有生命周期，所以要有一套跟进它的办法。**

笔记第 8 章第 3 条说的「长时协作与异步交互：支持多轮对话、任务委派、
进度更新、流式响应」，落地就是这个 `wait()` + `send_message()` 的组合。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from protocol import (
    INPUT_REQUIRED,
    TERMINAL_STATES,
    AgentCard,
    AgentSkill,
    Message,
    Part,
    STATE_LABELS,
    Task,
)
from server import AgentDirectory


@dataclass
class TaskUpdate:
    """一条进度事件。

    真实 A2A 里这是 SSE 推过来的（`capabilities.streaming`）；
    这里用轮询把它取出来，形状是一样的。
    """

    task_id: str
    state: str
    text: str
    is_terminal: bool

    def render(self) -> str:
        label = STATE_LABELS.get(self.state, self.state)
        return f"[{label}] {self.text}"


@dataclass
class WaitResult:
    """跟进一个任务之后拿到了什么。"""

    task: Task
    updates: list[TaskUpdate] = field(default_factory=list)
    reason: str = ""  # completed / needs_input / max_steps / failed

    @property
    def needs_input(self) -> bool:
        return self.reason == "needs_input"

    @property
    def completed(self) -> bool:
        return self.reason == "completed"


class A2AClient:
    """一个 Host 用来跟别的 Agent 打交道的客户端。"""

    def __init__(self, directory: AgentDirectory, *, auth_token: str = ""):
        self.directory = directory
        self.auth_token = auth_token
        self.requests_sent = 0

    # -- 发现 ---------------------------------------------------------------

    def discover(self, url: str) -> AgentCard:
        """拉一张 Agent 卡片。

        MCP 里对应的是 `initialize` + `tools/list`，但意图不同：
        MCP 的 tools/list 问的是「你能做什么**操作**」，
        A2A 的 agent card 问的是「你是个什么样的**角色**」——
        后者还带着身份、鉴权方式、服务方这些 MCP 根本不关心的信息。
        """
        for card in self.directory.cards():
            if card.url == url:
                return card
        raise KeyError(f"没有找到 {url} 的卡片")

    def route(self, request: str) -> list[tuple[AgentCard, AgentSkill]]:
        """按请求内容找候选。

        这一步在 MCP 里**不存在**。MCP 的 Server 是你自己配的，
        你写代码的时候就知道该连哪个；而 A2A 的 Agent 是运行时才知道的，
        所以「该派给谁」变成了一个真问题。
        """
        return self.directory.route(request)

    # -- 派发与跟进 ---------------------------------------------------------

    def send_task(
        self,
        card: AgentCard,
        text: str,
        *,
        context_id: str = "",
        message_id: str = "",
        idempotent: bool = False,
    ) -> Task:
        """派发一个任务。立刻返回，**不等它干完**。

        显式传 `message_id` 是为了演示重试：同一个 id 重发两次，
        配合对方的幂等去重才不会把活干两遍（演示 6）。
        """
        self.requests_sent += 1
        agent = self.directory.resolve(card)
        message = Message(
            role="user",
            parts=[Part.of_text(text)],
            message_id=message_id or f"req-{self.requests_sent}",
            context_id=context_id,
        )
        return agent.send_task(message, idempotent=idempotent)  # type: ignore[attr-defined]

    def wait(self, task: Task, *, max_steps: int = 12) -> WaitResult:
        """跟进任务，直到它完成、失败、或者停下来等你回话。

        这就是「异步」在客户端这一侧的样子：一个循环。
        它和 `call_tool()` 那种「一行拿到结果」的写法，感觉完全不同——
        因为**对端在做的事本来就不同**。
        """
        agent = self.directory.resolve_by_id(task.agent_id)
        updates: list[TaskUpdate] = []
        seen = len(task.history)

        for _ in range(max_steps):
            if task.state in TERMINAL_STATES:
                break
            if task.state == INPUT_REQUIRED:
                return WaitResult(task, updates, "needs_input")

            agent.step(task.id)  # type: ignore[attr-defined]

            # 把新产生的 agent 消息当成进度事件收下来
            for msg in task.history[seen:]:
                if msg.role == "agent":
                    updates.append(
                        TaskUpdate(task.id, task.state, msg.text, task.is_terminal)
                    )
            seen = len(task.history)

            if task.state in TERMINAL_STATES:
                break
            if task.state == INPUT_REQUIRED:
                return WaitResult(task, updates, "needs_input")

        if task.state in TERMINAL_STATES:
            reason = "completed" if task.state == "completed" else "failed"
        else:
            reason = "max_steps"
        return WaitResult(task, updates, reason)

    def send_message(self, task: Task, text: str) -> Task:
        """补充输入，让停在 INPUT_REQUIRED 的任务接着跑。"""
        self.requests_sent += 1
        agent = self.directory.resolve_by_id(task.agent_id)
        message = Message(
            role="user",
            parts=[Part.of_text(text)],
            message_id=f"req-{self.requests_sent}",
            task_id=task.id,
            context_id=task.context_id,
        )
        return agent.send_message(task.id, message)  # type: ignore[attr-defined]

    def cancel(self, task: Task) -> Task:
        agent = self.directory.resolve_by_id(task.agent_id)
        return agent.cancel_task(task.id)  # type: ignore[attr-defined]

    # -- 跑完一整件事（把上面几步串起来）------------------------------------

    def run_task(
        self,
        card: AgentCard,
        text: str,
        *,
        on_input_required: Callable[[Task], str] | None = None,
    ) -> WaitResult:
        """派发 → 跟进 → （需要的话）补充输入 → 再跟进，直到终态。

        `on_input_required` 是人（或上层编排器）回答问题的钩子。
        传 None 的话，遇到 INPUT_REQUIRED 就停在那里交回去。
        """
        task = self.send_task(card, text)
        result = self.wait(task)
        # 进度事件要**累积**。每调一次 wait() 都是新开一轮，
        # 只看最后一次的返回值，前面那几轮的进度就丢了——
        # 而「进度」恰恰是 A2A 相对 MCP 多出来的东西，丢了它这个演示就没意义了。
        updates = list(result.updates)
        while result.needs_input and on_input_required is not None:
            reply = on_input_required(task)
            self.send_message(task, reply)
            result = self.wait(task)
            updates.extend(result.updates)
        result.updates = updates
        return result

    # -- 用 MCP 风格调一次（拿来做对照）-------------------------------------

    def call_tool_directly(self, tool: str, **arguments: Any) -> str:
        """MCP 风格的调用：一行拿到结果。

        这不是 A2A 的一部分——它是拿来当对照组的，
        好让你看清「调用工具」和「委派任务」在代码上差多少。
        """
        for card in self.directory.cards():
            agent = self.directory.resolve(card)
            mcp = getattr(agent, "mcp", None)
            if mcp is not None:
                self.requests_sent += 1
                return mcp.call_tool(tool, arguments).text
        return "（没有可用的 MCP 连接）"
