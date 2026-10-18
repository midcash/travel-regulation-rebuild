"""A2A 协议：消息、任务状态机、Agent 卡片。对应笔记第 8 章。

笔记第 8 章很短（只有一页），但它点出的四件事每一件都得落到代码里：

    1. 互操作性          通过标准化的消息格式和通信机制
                         （常用技术：JSON-RPC、HTTP、SSE）
    2. Agent 卡片        每个 Agent 通过「卡片」描述自己的能力、接口、
                         身份和安全策略，方便其他 Agent 发现和调用
    3. 长时协作与异步交互  支持多轮对话、任务委派、进度更新、流式响应
    4. 内部实现不透明     调用方只知道对方是一个 Agent，
                         不关心它内部用什么模型或工具

第 3 条是这个文件和 p06（MCP）拉开距离的地方。**MCP 里没有「任务」这个东西**——
`tools/call` 要么返回结果，要么返回错误，中间没有第三种状态。
A2A 的核心恰恰是中间那些状态：一个任务可以跑十分钟，
中途可以说「我需要你补充一点信息」，然后接着跑。

--------------------------------------------------------------------------
A2A 与 MCP 的分工（这是整个原型想讲清楚的一件事）

    MCP   Agent ──工具/资源     垂直：Agent 向下够到能力
    A2A   Agent ──Agent         水平：Agent 之间互相委派

    对端是什么    MCP 是无状态、确定性、快的工具
                 A2A 是有状态、概率性、慢的另一个 Agent
    调用语义      MCP: tools/call  → 立刻拿到结果
                 A2A: tasks/send  → 拿到一个 task_id，之后慢慢等
    生命周期      毫秒~秒  vs  分钟~天
    失败语义      MCP 重试通常安全（工具是幂等的）
                 A2A 重试可能导致**重复执行**（对方是个 Agent，它会真的再干一遍）
    发现机制      MCP: tools/list（列工具清单）
                 A2A: agent card（声明能力、身份、鉴权方式）
    结果形态      MCP 结构化数据
                 A2A 任务 + 进度事件 + 产物（artifact）

一句话：**MCP 让你调用工具，A2A 让你委派工作。**
委派出去的工作是有生命周期的，所以需要状态机。
--------------------------------------------------------------------------
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

PROTOCOL_VERSION = "0.2.5"


# ==========================================================================
# 任务状态机
# ==========================================================================

SUBMITTED = "submitted"
WORKING = "working"
INPUT_REQUIRED = "input-required"
AUTH_REQUIRED = "auth-required"
COMPLETED = "completed"
CANCELED = "canceled"
FAILED = "failed"
REJECTED = "rejected"

TERMINAL_STATES = {COMPLETED, CANCELED, FAILED, REJECTED}

#: 允许的状态迁移。
#:
#: 这张表是 A2A 和「一次 RPC 调用」的本质区别所在。
#: RPC 调用的状态空间是 {成功, 失败}，两格；这里八格，而且不是随便跳的。
#:
#: 两处最值得注意：
#:   * INPUT_REQUIRED 是**双向**的——它回到 WORKING，而不是直接结束。
#:     这一格就是「Agent 中途问你一个问题」，工具调用里根本没有对应物。
#:   * 终态是**吸收态**，出不去。一个任务一旦 completed，
#:     就不能再被「重试」拉回 working——这正是重试语义比 MCP 复杂的原因。
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    SUBMITTED: {WORKING, REJECTED, CANCELED, AUTH_REQUIRED},
    WORKING: {INPUT_REQUIRED, AUTH_REQUIRED, COMPLETED, FAILED, CANCELED},
    INPUT_REQUIRED: {WORKING, FAILED, CANCELED},
    AUTH_REQUIRED: {WORKING, FAILED, CANCELED},
    COMPLETED: set(),
    CANCELED: set(),
    FAILED: set(),
    REJECTED: set(),
}

STATE_LABELS = {
    SUBMITTED: "已提交",
    WORKING: "处理中",
    INPUT_REQUIRED: "等待补充输入",
    AUTH_REQUIRED: "等待鉴权",
    COMPLETED: "已完成",
    CANCELED: "已取消",
    FAILED: "失败",
    REJECTED: "被拒绝",
}


class StateTransitionError(Exception):
    """非法的状态迁移。

    注意这是**协议级**错误，和 p06 里那两层错误是同一个道理：
    它不该被当成业务失败，而是「你的客户端用错了」。
    """

    def __init__(self, task_id: str, current: str, attempted: str):
        allowed = ALLOWED_TRANSITIONS.get(current, set())
        allowed_text = "、".join(STATE_LABELS.get(s, s) for s in allowed) or "（终态，出不去）"
        super().__init__(
            f"任务 {task_id} 现在是「{STATE_LABELS.get(current, current)}」，"
            f"不能迁移到「{STATE_LABELS.get(attempted, attempted)}」。"
            f"允许的下一个状态：{allowed_text}"
        )
        self.task_id = task_id
        self.current = current
        self.attempted = attempted


# ==========================================================================
# 消息
# ==========================================================================


@dataclass
class Part:
    """消息的一个片段。

    A2A 的消息不是一段字符串，而是 Part 的列表——
    因为 Agent 之间传的东西可能是文本、结构化数据、或者一个文件。
    笔记第 8 章说的「标准化的消息格式」，具体就标准化在这里。
    """

    kind: str  # text / data / file
    text: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def of_text(text: str) -> "Part":
        return Part("text", text=text)

    @staticmethod
    def of_data(**data: Any) -> "Part":
        return Part("data", data=data)

    def render(self) -> str:
        if self.kind == "text":
            return self.text
        return json.dumps(self.data, ensure_ascii=False)

    def to_json(self) -> dict[str, Any]:
        if self.kind == "text":
            return {"kind": "text", "text": self.text}
        return {"kind": "data", "data": self.data}


@dataclass
class Message:
    role: str  # user / agent
    parts: list[Part]
    message_id: str
    task_id: str = ""
    context_id: str = ""

    @property
    def text(self) -> str:
        return " ".join(part.render() for part in self.parts)

    def to_json(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "parts": [part.to_json() for part in self.parts],
            "messageId": self.message_id,
            "taskId": self.task_id,
            "contextId": self.context_id,
        }


@dataclass
class Artifact:
    """产物。

    这是 A2A 独有的一层：委派出去的工作，干完了得**交付**点什么。
    MCP 的 tools/call 返回的是一份数据，没有「交付物」这个概念——
    因为工具不会「完成一项工作」，它只是「被调用了一次」。
    """

    name: str
    parts: list[Part]
    artifact_id: str
    description: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "artifactId": self.artifact_id,
            "name": self.name,
            "description": self.description,
            "parts": [part.to_json() for part in self.parts],
        }


@dataclass
class TaskStatus:
    state: str
    message: Message | None = None
    timestamp: float = 0.0

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "state": self.state,
            "timestamp": self.timestamp,
        }
        if self.message:
            payload["message"] = self.message.to_json()
        return payload


@dataclass
class Task:
    """一次委派出去的工作。

    注意它和 MCP 的 `tools/call` 返回值的形状差异：
    tools/call 返回的是**结果**（一次性的），
    Task 返回的是**过程 + 结果**（history 是过程，artifacts 是结果）。
    """

    id: str
    context_id: str
    status: TaskStatus
    history: list[Message] = field(default_factory=list)
    artifacts: list[Artifact] = field(default_factory=list)
    #: 被委派的那个 Agent 的标识（A2A 里叫 agentId）。
    #: Host 只认这个 id 和它的卡片，对它的内部一无所知。
    agent_id: str = ""

    @property
    def state(self) -> str:
        return self.status.state

    @property
    def is_terminal(self) -> bool:
        return self.status.state in TERMINAL_STATES

    def can_transition_to(self, new_state: str) -> bool:
        return new_state in ALLOWED_TRANSITIONS.get(self.status.state, set())

    def transition(self, new_state: str, message: Message | None = None) -> None:
        """迁移状态。非法迁移直接抛错——**不静默接受**。"""
        if not self.can_transition_to(new_state):
            raise StateTransitionError(self.id, self.status.state, new_state)
        self.status = TaskStatus(state=new_state, message=message, timestamp=time.time())
        if message:
            self.history.append(message)

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "contextId": self.context_id,
            "status": self.status.to_json(),
            "history": [m.to_json() for m in self.history],
            "artifacts": [a.to_json() for a in self.artifacts],
            "agentId": self.agent_id,
        }

    def progress_lines(self) -> list[str]:
        """把 history 里 agent 说的话抽出来，当成进度看。"""
        return [m.text for m in self.history if m.role == "agent" and not m.message_id.startswith("reply")]


# ==========================================================================
# Agent 卡片
# ==========================================================================

#: 卡片里 capabilities 的字段。MCP 的 initialize 也有 capabilities，
#: 但两者说的不是一回事：MCP 声明的是「我支持哪些协议特性」，
#: A2A 声明的还包含「我这个 Agent 会干什么」——那是 skills 的事。
CAPABILITY_FIELDS = {
    "streaming": "支持流式响应（SSE）",
    "pushNotifications": "支持主动推送通知",
    "stateTransitionHistory": "保留完整的状态迁移历史",
}


@dataclass
class AgentSkill:
    """一项能力。**这是 Host 决定「该派给谁」的依据。**"""

    id: str
    name: str
    description: str
    tags: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)

    def matches(self, query: str) -> bool:
        haystack = f"{self.name} {self.description} {' '.join(self.tags)} {' '.join(self.examples)}"
        return any(token and token in haystack for token in query.split())

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "tags": self.tags,
            "examples": self.examples,
        }


@dataclass
class AgentCard:
    """Agent 卡片（笔记第 8 章第 2 条）。

    笔记的原话：

        每个 Agent 通过「卡片」描述自己的**能力、接口、身份和安全策略**，
        方便其他 Agent 发现和调用。

    这四个词对应卡片上的四组字段，一个都不能少：

        能力       skills
        接口       url / preferredTransport / protocolVersion
        身份       provider / name / version
        安全策略   securitySchemes / security

    **注意卡片上没有的东西**：没有「我用什么模型」「我有哪些工具」「我的提示词长什么样」。
    这就是笔记第 8 章第 4 条说的「内部实现不透明」——
    它是设计目标，不是巧合。调用方只需要知道**你能干什么**，
    不需要知道**你怎么干的**。后面演示 5 会验证这一点。
    """

    name: str
    description: str
    url: str
    version: str = "1.0.0"
    provider: str = "未署名"
    skills: list[AgentSkill] = field(default_factory=list)
    capabilities: dict[str, bool] = field(default_factory=dict)
    default_input_modes: list[str] = field(default_factory=lambda: ["text/plain"])
    default_output_modes: list[str] = field(default_factory=lambda: ["text/plain"])
    security_schemes: dict[str, Any] = field(default_factory=dict)
    security: list[dict[str, list[str]]] = field(default_factory=list)
    protocol_version: str = PROTOCOL_VERSION

    def find_skill(self, query: str) -> AgentSkill | None:
        for skill in self.skills:
            if skill.matches(query):
                return skill
        return None

    def describe_capabilities(self) -> str:
        on = [CAPABILITY_FIELDS.get(k, k) for k, v in self.capabilities.items() if v]
        return "、".join(on) if on else "（无）"

    def describe_security(self) -> str:
        if not self.security_schemes:
            return "（无需鉴权）"
        names = list(self.security_schemes)
        return "、".join(names)

    def to_json(self) -> dict[str, Any]:
        return {
            "protocolVersion": self.protocol_version,
            "name": self.name,
            "description": self.description,
            "url": self.url,
            "version": self.version,
            "provider": {"organization": self.provider},
            "capabilities": self.capabilities,
            "defaultInputModes": self.default_input_modes,
            "defaultOutputModes": self.default_output_modes,
            "skills": [s.to_json() for s in self.skills],
            "securitySchemes": self.security_schemes,
            "security": self.security,
        }
