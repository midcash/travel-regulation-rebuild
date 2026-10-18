"""Agent 框架的共同内核与四步选型原型。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Framework:
    name: str
    method: str
    strengths: tuple[str, ...]
    ecosystems: tuple[str, ...]


@dataclass(frozen=True)
class ProjectBrief:
    team: str
    scenario: str
    cloud: str = ""
    model: str = ""
    low_code: bool = False


FRAMEWORKS = (
    Framework("LangChain", "组件组装", ("通用模型/工具生态", "快速原型"), ("Python", "TypeScript")),
    Framework("LangGraph", "状态图编排", ("复杂状态", "暂停恢复", "分支循环"), ("Python", "TypeScript")),
    Framework("DeepAgents", "长任务 Harness", ("规划", "工作区", "上下文管理"), ("Python",)),
    Framework("OpenAI Agents SDK", "Agent + Handoff", ("OpenAI", "语音", "快速工具调用"), ("Python",)),
    Framework("Claude Agent SDK", "环境 Harness", ("代码库", "文件", "Shell"), ("Python", "TypeScript")),
    Framework("Google ADK", "确定性编排 + Agent", ("顺序/并行/循环", "Google Cloud"), ("Python", "TypeScript", "Java", "Go")),
    Framework("Microsoft Agent Framework", "企业 Agent", ("Azure", "Microsoft 365", ".NET"), ("Python", ".NET")),
    Framework("CrewAI", "角色化团队", ("角色", "任务", "多 Agent"), ("Python",)),
    Framework("PydanticAI", "类型安全 Agent", ("结构化输出", "Python API"), ("Python",)),
    Framework("LlamaIndex", "数据/RAG 管道", ("数据连接", "索引", "检索"), ("Python", "TypeScript")),
    Framework("Dify", "平台工作流", ("低代码", "知识库", "发布"), ("Web",)),
    Framework("AgentScope", "国内企业多 Agent", ("国产模型", "Java", "私有化"), ("Python", "Java")),
)


def common_kernel() -> tuple[str, ...]:
    return (
        "模型适配",
        "工具封装与执行",
        "Agent Loop",
        "会话/记忆/上下文",
        "流程控制与人工介入",
        "Trace/评测/可靠性",
    )


def select(brief: ProjectBrief) -> list[tuple[Framework, str]]:
    """按硬约束 -> 场景 -> 云/模型偏好筛选，返回有理由的候选。"""
    if brief.low_code:
        return [(next(item for item in FRAMEWORKS if item.name == "Dify"), "产品/运营参与搭建并希望快速发布")]
    candidates = list(FRAMEWORKS)
    if brief.team in {".NET", "Java"}:
        candidates = [item for item in candidates if brief.team in item.ecosystems]
    if brief.cloud == "Azure":
        preferred = [item for item in candidates if item.name == "Microsoft Agent Framework"]
        if preferred:
            return [(preferred[0], "Azure 硬约束")]
    if brief.cloud == "Google Cloud":
        preferred = [item for item in candidates if item.name == "Google ADK"]
        if preferred:
            return [(preferred[0], "Google Cloud 硬约束")]
    keywords = {
        "复杂状态": "LangGraph",
        "长任务": "DeepAgents",
        "代码库": "Claude Agent SDK",
        "RAG": "LlamaIndex",
        "多 Agent": "CrewAI",
        "类型安全": "PydanticAI",
        "OpenAI": "OpenAI Agents SDK",
        "国产模型": "AgentScope",
        "顺序并行循环": "Google ADK",
    }
    selected = []
    for keyword, name in keywords.items():
        if keyword in brief.scenario or keyword in brief.model:
            framework = next((item for item in candidates if item.name == name), None)
            if framework is None:
                # 例如 Java 团队命中“多 Agent”时，CrewAI 不在硬约束候选中，
                # 继续看“国产模型/企业生态”等更具体的匹配，而不是直接报错。
                continue
            selected.append((framework, f"匹配核心问题：{keyword}"))
    if selected:
        return selected
    fallback = next((item for item in candidates if item.name == "LangChain"), candidates[0])
    return [(fallback, "通用模型、工具和检索集成")]
