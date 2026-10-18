"""两个 A2A Agent：一个差旅 Agent，一个报销 Agent。

这两个 Agent 是**故意写得完全不一样**的：

    TravelAgent    分五步干活，中途会停下来问用户一个问题（input-required）
    ExpenseAgent   一步干完，从不中途提问

它们对外的接口却一模一样——都是「派一个任务给我，我给你 task_id」。
演示 5 会用这一点说明笔记第 8 章的第 4 条：**内部实现不透明**。

另外注意 TravelAgent 内部是怎么干活的：**它通过 MCP 调工具。**
这不是巧合，这是这两种协议在真实系统里的位置关系：

    Host ──A2A──> Travel Agent ──MCP──> 知识库 / 订单系统

A2A 在外层（Agent 之间），MCP 在内层（Agent 与能力之间）。
所以这个文件会 import p06 的 MCPServer —— 那是最直接的证据：
**它们不是竞品，是上下楼。**
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
from typing import Any

from protocol import (
    COMPLETED,
    INPUT_REQUIRED,
    SUBMITTED,
    WORKING,
    AgentCard,
    AgentSkill,
    Artifact,
    Message,
    Part,
    Task,
    TaskStatus,
)


# ==========================================================================
# 把 p06 的 MCP 实现借过来用
# ==========================================================================


_P06_CACHE: dict[str, Any] = {}


def load_p06_mcp() -> tuple[Any, Any]:
    """按文件路径加载 p06 的 MCPServer / MCPClient。

    跨原型 import 通常不是好习惯，但这里是有意的：
    「A2A Agent 内部用 MCP 调工具」这件事，只有**真的跑起来**才算证明。

    麻烦在于 p06 和 p10 都有 `protocol.py` / `client.py` / `server.py` ——
    直接 `from server import MCPServer` 会撞上 p10 自己的 server.py。
    所以这里按文件路径显式加载，并且：

      * 用 `importlib` 加载，**惰性**执行（第一次用到才加载）。
        不能放在模块顶层——那时候 p10 自己的 server.py 还在初始化，
        sys.modules 里是个半成品，临时替换很容易出事。
      * p06 内部是 `from protocol import ...` 这种短名互相引用的，
        所以加载期间要把短名临时指向 p06 的版本，加载完立刻还原。
    """
    if _P06_CACHE:
        return _P06_CACHE["server"], _P06_CACHE["client"]

    p06_dir = pathlib.Path(__file__).resolve().parents[1] / "p06_mcp"
    # 顺序是**依赖顺序**，不能随便排：p06 的 client.py 里有
    # `from server import MCPServer`，所以 server 必须先加载完。
    # 写成 ("protocol", "client", "server") 的话，加载 client 时
    # sys.modules["server"] 还是 p10 自己那个半成品，直接 ImportError。
    load_order = ("protocol", "server", "client")
    saved = {name: sys.modules.get(name) for name in load_order}
    try:
        for name in load_order:
            spec = importlib.util.spec_from_file_location(f"p06_{name}", p06_dir / f"{name}.py")
            assert spec and spec.loader
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module          # p06 内部按短名 import
            sys.modules[f"p06_{name}"] = module
            spec.loader.exec_module(module)
        _P06_CACHE["server"] = sys.modules["p06_server"]
        _P06_CACHE["client"] = sys.modules["p06_client"]
    finally:
        for name in load_order:
            sys.modules.pop(name, None)
            if saved[name] is not None:
                sys.modules[name] = saved[name]

    return _P06_CACHE["server"], _P06_CACHE["client"]


def make_mcp_client() -> Any:
    """造一个连到 p06 那个知识库服务端的 MCP 客户端。"""
    p06_server, p06_client = load_p06_mcp()
    client = p06_client.MCPClient(p06_client.InProcessTransport(p06_server.MCPServer()))
    client.initialize()
    return client


# ==========================================================================
# Agent 一：差旅改签（分步、会中途提问）
# ==========================================================================


class TravelAgent:
    """处理改签请求的 A2A Agent。

    它暴露给外面的是「任务」，藏起来的是「步骤」。
    Host 看到的只有状态迁移和进度消息，看不到它内部调了几次 MCP。
    """

    def __init__(self, mcp: Any = None):
        # 内部实现：通过 MCP 连一个知识库服务端（就是 p06 里那个 MCPServer）。
        # 换掉这一行，外面完全感觉不到 —— 这正是「内部实现不透明」。
        self.mcp = mcp if mcp is not None else make_mcp_client()
        self._tasks: dict[str, Task] = {}
        self._plans: dict[str, list[str]] = {}
        self.mcp_calls: list[str] = []
        #: messageId -> taskId。用来做幂等去重，演示 6 会用到。
        #: 网络超时之后客户端会重发，**没有这张表就会真的把活干第二遍**。
        self._seen_messages: dict[str, str] = {}

    # -- 卡片 ---------------------------------------------------------------

    def card(self) -> AgentCard:
        return AgentCard(
            name="差旅改签 Agent",
            description="处理火车票 / 机票的改签与退票，含会员权益计算。",
            url="https://travel.example.com/a2a/v1",
            version="1.4.0",
            provider="某某科技·差旅事业部",
            capabilities={"streaming": True, "stateTransitionHistory": True},
            skills=[
                AgentSkill(
                    id="change-ticket",
                    name="车票改签",
                    description="按订单号改签车次或座位，计算差价与手续费。",
                    tags=["改签", "车票", "订单"],
                    examples=["帮我把 T20260920-0001 改到明天上午"],
                ),
                AgentSkill(
                    id="refund-ticket",
                    name="车票退票",
                    description="按订单号退票，按会员等级计算手续费。",
                    tags=["退票", "退款", "订单"],
                    examples=["把 T20260920-0001 退掉"],
                ),
            ],
            security_schemes={"oauth2": {"type": "oauth2", "flows": {"clientCredentials": {}}}},
            security=[{"oauth2": ["travel.write"]}],
        )

    # -- 任务接口（对外只有这四个方法）--------------------------------------

    def send_task(self, message: Message, *, idempotent: bool = False) -> Task:
        """派发一个任务。**立刻返回，不等它干完。**

        `idempotent=True` 时按 messageId 去重：同一个 messageId 重复派发，
        返回的是**原来那个任务**，不会真的再干一遍。
        演示 6 会对比开与不开的差别。

        为什么 MCP 不需要操这个心？因为工具是幂等的（`readOnlyHint` / `idempotentHint`
        就是为了声明这件事），重试一次只是重读一遍数据。
        而 A2A 的对端是个 Agent —— 它会真的**再改一次签**。
        """
        if idempotent and message.message_id in self._seen_messages:
            return self._tasks[self._seen_messages[message.message_id]]
        task_id = f"task-{len(self._tasks) + 1:03d}"
        self._seen_messages[message.message_id] = task_id
        task = Task(
            id=task_id,
            context_id=message.context_id or f"ctx-{task_id}",
            status=TaskStatus(state=SUBMITTED),
            agent_id=self.card().name,
        )
        message.task_id = task_id
        task.history.append(message)
        self._tasks[task_id] = task
        self._plans[task_id] = ["查订单", "查政策", "问偏好", "算差价", "出变更单"]
        return task

    def step(self, task_id: str) -> Task:
        """把这个任务往前推一步。

        真实系统里这一步是后台事件循环在跑、或者 Host 在轮询；
        这里显式暴露出来，是为了让你能在调试器里一步一步走。
        """
        task = self._tasks[task_id]
        if task.is_terminal or task.state == INPUT_REQUIRED:
            return task

        if task.state == SUBMITTED:
            task.transition(WORKING, self._say(task, "开始处理，先核对订单。"))

        plan = self._plans[task_id]
        if not plan:
            self._finish(task)
            return task

        action = plan.pop(0)
        handler = {
            "查订单": self._lookup_order,
            "查政策": self._lookup_policy,
            "问偏好": self._ask_preference,
            "算差价": self._compute_price,
            "出变更单": lambda t: None,
        }[action]
        handler(task)
        return task

    def send_message(self, task_id: str, message: Message) -> Task:
        """补充输入。

        **这是 A2A 独有的一格**：任务在 INPUT_REQUIRED 停着，等这一句话回来，
        然后从原地接着跑。MCP 的 tools/call 里没有对应物 ——
        工具不会在中途停下来问你一个问题。
        """
        task = self._tasks[task_id]
        message.task_id = task_id
        task.history.append(message)
        if task.state == INPUT_REQUIRED:
            task.transition(WORKING, self._say(task, f"收到，按「{message.text}」继续。"))
        return task

    def cancel_task(self, task_id: str) -> Task:
        task = self._tasks[task_id]
        if not task.is_terminal:
            task.transition(CANCELED, self._say(task, "已取消。"))
        return task

    def get_task(self, task_id: str) -> Task:
        return self._tasks[task_id]

    # -- 内部实现（下面这些外面一概看不到）----------------------------------

    def _say(self, task: Task, text: str) -> Message:
        return Message(
            role="agent",
            parts=[Part.of_text(text)],
            message_id=f"agent-{len(task.history) + 1}",
            task_id=task.id,
            context_id=task.context_id,
        )

    def _mcp(self, tool: str, **arguments: object) -> str:
        """通过 MCP 调一次工具。注意这是**内部实现**，卡片上不写这些。"""
        self.mcp_calls.append(tool)
        result = self.mcp.call_tool(tool, arguments)
        if result.is_error:
            return f"（工具报错：{result.text[:60]}）"
        return result.text.replace("\n", " ")

    def _lookup_order(self, task: Task) -> None:
        info = self._mcp("query_knowledge", collection="travel_policy",
                         question="退票规定 改签手续费", top_k=2)
        task.history.append(self._say(task, f"订单核对完成。参考规定：{info[:70]}"))

    def _lookup_policy(self, task: Task) -> None:
        info = self._mcp("query_knowledge", collection="member_rules",
                         question="白金卡 免费改签", top_k=1)
        task.history.append(self._say(task, f"会员权益已确认：{info[:70]}"))

    def _ask_preference(self, task: Task) -> None:
        question = Message(
            role="agent",
            parts=[Part.of_text("改签到明天上午的三趟车里，您想坐哪一趟？"),
                   Part.of_data(options=["G101 07:00", "G103 08:30", "G105 10:00"])],
            message_id=f"agent-{len(task.history) + 1}",
            task_id=task.id,
            context_id=task.context_id,
        )
        task.transition(INPUT_REQUIRED, question)

    def _compute_price(self, task: Task) -> None:
        task.history.append(self._say(task, "改签差价 0 元（白金卡免费改签，同价位车次）。"))

    def _finish(self, task: Task) -> None:
        task.artifacts.append(
            Artifact(
                name="行程变更单",
                description="改签结果，可直接用于报销",
                artifact_id=f"artifact-{task.id}",
                parts=[
                    Part.of_text("订单 T20260920-0001 已改签至 G103（明天 08:30），差价 0 元。"),
                    Part.of_data(order_id="T20260920-0001", new_train="G103", fare_diff=0),
                ],
            )
        )
        task.transition(COMPLETED, self._say(task, "改签完成，变更单已生成。"))


# ==========================================================================
# Agent 二：报销审核（一步干完，从不提问）
# ==========================================================================


class ExpenseAgent:
    """另一个「厂商」的 Agent。

    它和 TravelAgent 的内部实现完全不同：没有 MCP、没有分步、
    没有状态迁移历史。但**它的卡片长得一模一样**——
    一样有 skills、有 url、有鉴权声明。

    Host 那边换掉它，代码一行都不用改。这就是「内部实现不透明」的好处。
    """

    def __init__(self):
        self._tasks: dict[str, Task] = {}
        self.calls = 0
        self._seen_messages: dict[str, str] = {}

    def card(self) -> AgentCard:
        return AgentCard(
            name="报销审核 Agent",
            description="审核差旅报销单，校验发票抬头与金额是否合规。",
            url="https://expense.example.com/a2a",
            version="0.9.1",
            provider="另一家公司",
            capabilities={"streaming": False},  # 注意：它不支持流式
            skills=[
                AgentSkill(
                    id="audit-expense",
                    name="报销单审核",
                    description="校验发票抬头、金额上限、附件是否齐全。",
                    tags=["报销", "审核", "发票"],
                    examples=["审一下这张住宿发票"],
                ),
            ],
            security_schemes={"apiKey": {"type": "apiKey", "in": "header", "name": "X-API-Key"}},
            security=[{"apiKey": []}],
        )

    def send_task(self, message: Message, *, idempotent: bool = False) -> Task:
        # 幂等去重是**协议层**的要求，不是某个 Agent 的实现细节，
        # 所以两个 Agent 都得实现它——虽然它们内部完全不一样。
        if idempotent and message.message_id in self._seen_messages:
            return self._tasks[self._seen_messages[message.message_id]]
        task_id = f"exp-{len(self._tasks) + 1:03d}"
        self._seen_messages[message.message_id] = task_id
        task = Task(
            id=task_id,
            context_id=message.context_id or f"ctx-{task_id}",
            status=TaskStatus(state=SUBMITTED),
            agent_id=self.card().name,
        )
        message.task_id = task_id
        task.history.append(message)
        self._tasks[task_id] = task
        return task

    def step(self, task_id: str) -> Task:
        """一步干完。没有中途提问，没有中间状态。"""
        task = self._tasks[task_id]
        if task.is_terminal:
            return task
        self.calls += 1
        task.transition(WORKING)
        task.artifacts.append(
            Artifact(
                name="审核结论",
                artifact_id=f"artifact-{task.id}",
                parts=[Part.of_data(approved=True, reason="抬头与金额均合规")],
            )
        )
        task.transition(
            COMPLETED,
            Message(role="agent", parts=[Part.of_text("审核通过。")],
                    message_id="agent-1", task_id=task.id, context_id=task.context_id),
        )
        return task

    def send_message(self, task_id: str, message: Message) -> Task:
        task = self._tasks[task_id]
        task.history.append(message)
        return task

    def cancel_task(self, task_id: str) -> Task:
        task = self._tasks[task_id]
        if not task.is_terminal:
            task.transition(CANCELED)
        return task

    def get_task(self, task_id: str) -> Task:
        return self._tasks[task_id]


# ==========================================================================
# 目录服务：Agent 之间怎么「发现」彼此
# ==========================================================================


class AgentDirectory:
    """一个极简的 Agent 目录。

    笔记第 8 章第 1 条说 A2A 的目标是「让不同 Agent 可以发现彼此并交换任务」。
    「发现」这一步在 MCP 里不存在——MCP 的 Server 是**配置好的**，
    你在写代码的时候就知道要连哪个。而 A2A 的 Agent 是**运行时找的**。

    这个差别会带来一个 MCP 完全没有的问题：**该派给谁？**
    """

    def __init__(self) -> None:
        self._agents: list[tuple[AgentCard, object]] = []

    def register(self, agent: object) -> AgentCard:
        card = agent.card()  # type: ignore[attr-defined]
        self._agents.append((card, agent))
        return card

    def cards(self) -> list[AgentCard]:
        return [card for card, _ in self._agents]

    def resolve(self, card: AgentCard) -> object:
        for known, agent in self._agents:
            if known.url == card.url:
                return agent
        raise KeyError(f"目录里没有 {card.url}")

    def resolve_by_id(self, agent_id: str) -> object:
        """任务上记的是 agentId，用它在目录里找回那个 Agent。

        真实系统里这一步是一次网络请求（按 agentId 查注册中心）；
        这里退化成一次本地查表，但**契约是一样的**：
        客户端手里只有一个 id，它不持有对方对象的引用。
        """
        for card, agent in self._agents:
            if card.name == agent_id:
                return agent
        raise KeyError(f"目录里没有 id 为 {agent_id!r} 的 Agent")

    def route(self, request: str) -> list[tuple[AgentCard, AgentSkill]]:
        """按请求内容找候选 Agent。**返回的是列表，不是一个。**

        因为「谁最合适」在这个层面上是判断不了的——
        一个请求可能同时匹配多个 Agent 的多项技能，
        真正的选择权应该留给调用方（Host / 编排器）。
        """
        hits: list[tuple[AgentCard, AgentSkill]] = []
        for card in self.cards():
            skill = card.find_skill(request)
            if skill:
                hits.append((card, skill))
        return hits
