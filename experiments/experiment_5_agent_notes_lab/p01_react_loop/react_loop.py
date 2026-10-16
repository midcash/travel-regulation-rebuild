"""ReAct / Agentic Loop 的骨架实现。

对应笔记 9.2 节那段伪代码，这里是它的逐行落地：

    while（任务未结束）{
        模型回答 = 调用模型（用户问题 + 对话历史 + 工具列表）
        if（模型要求调用工具）{
            工具结果 = 框架执行工具()
            将工具结果加入对话历史
            continue
        }
        // 没有工具调用，普通文本就是最终答案，结束流程
    }

三个角色要分清楚，这是 ReAct 最容易被面试官追问的地方：
  - Reasoning（推理）：模型干的。它读上下文，决定"下一步做什么、做完了没有、要不要调工具"。
  - Act（行动）：模型选出工具和参数，**框架**负责真正执行。
  - Observation（观察）：框架把真实结果回注对话历史，模型据此继续推理。

这个文件只写"框架"那一半（Harness）。模型那一半在 main.py 的策略函数里，
这样在调试器里你能清清楚楚看到哪一步是模型、哪一步是框架。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from common.llm import LLM, LLMResponse, Message, ToolCall, count_message_tokens
from common.tools import ToolRegistry, ToolResult

# 系统提示词：笔记 9.2 说"提示词告诉 AI，让他思考下一步做什么，任务做完没有，
# 要不要调用工具"。这三件事就是这段提示词要覆盖的全部内容。
DEFAULT_SYSTEM_PROMPT = """你是一个会使用工具的助手。

规则：
1. 先判断当前信息够不够回答问题。不够就调用工具，够了就直接回答。
2. 需要精确数字时用 calculator，不要心算。
3. 拿到工具结果后，先判断结果是否有效、任务是否完成，再决定继续调工具还是给出答案。
4. 不需要调用工具时，直接输出最终答案，不要输出多余的话。"""


@dataclass
class Step:
    """循环的一圈。调试时最常看的就是这个结构。"""

    index: int
    response: LLMResponse
    results: list[ToolResult] = field(default_factory=list)

    @property
    def called_tools(self) -> list[str]:
        return [call.name for call in self.response.tool_calls]

    @property
    def is_final(self) -> bool:
        return not self.response.tool_calls


@dataclass
class LoopResult:
    answer: str
    steps: list[Step]
    messages: list[Message]
    stopped_reason: str  # final_answer / max_steps / repeated_call
    llm_calls: int = 0
    tool_calls: int = 0
    context_tokens: int = 0

    @property
    def ok(self) -> bool:
        return self.stopped_reason == "final_answer"


class ReActAgent:
    """Harness 循环。

    刻意写的很短：真正负责"智能"的是模型，框架只做四件事——
    组织上下文、执行工具、回注结果、判断何时停。
    """

    def __init__(
        self,
        llm: LLM,
        registry: ToolRegistry,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        max_steps: int = 6,
        on_step: Callable[[Step], None] | None = None,
    ):
        self.llm = llm
        self.registry = registry
        self.system_prompt = system_prompt
        self.max_steps = max_steps
        self.on_step = on_step

    def run(self, user_input: str) -> LoopResult:
        # 第 1 步：接收用户 Prompt、系统指令、对话历史和工具定义（笔记 9.2 第 1 条）
        messages: list[Message] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user_input},
        ]
        tools = self.registry.schemas()
        steps: list[Step] = []
        seen_calls: set[str] = set()

        while True:
            # 第 2 步：模型分析上下文，生成普通文本或结构化的工具调用请求
            response = self.llm.chat(messages, tools)
            step = Step(index=len(steps) + 1, response=response)
            messages.append(response.as_message())

            if not response.tool_calls:
                # 第 5 步：没有工具调用，普通文本就是最终结果，结束流程
                steps.append(step)
                if self.on_step:
                    self.on_step(step)
                return self._finish(response.content, steps, messages, "final_answer")

            # 第 4 步：把工具结果回注到对话历史，交给模型继续推理
            repeated = False
            for call in response.tool_calls:
                signature = self._signature(call)
                if signature in seen_calls:
                    # 笔记 13.1.4 提到的"无进展检测"：同一个调用反复出现说明模型卡住了，
                    # 再转下去只是烧钱。这是有界循环的一部分。
                    repeated = True
                seen_calls.add(signature)
                result = self.registry.call(call.name, call.arguments)
                step.results.append(result)
                messages.append(result.to_message(call.id, call.name))

            steps.append(step)
            if self.on_step:
                self.on_step(step)

            if repeated:
                return self._finish(
                    "（循环被中止：模型重复调用了同一个工具且没有进展）",
                    steps,
                    messages,
                    "repeated_call",
                )
            if len(steps) >= self.max_steps:
                # 笔记 13.1.4："每个正经的 loop 都得配一个心跳机制——最大迭代次数"
                return self._finish(
                    f"（循环被中止：达到最大轮次 {self.max_steps}）",
                    steps,
                    messages,
                    "max_steps",
                )

    def _signature(self, call: ToolCall) -> str:
        return f"{call.name}:{json.dumps(call.arguments, sort_keys=True, ensure_ascii=False, default=str)}"

    def _finish(
        self, answer: str, steps: list[Step], messages: list[Message], reason: str
    ) -> LoopResult:
        return LoopResult(
            answer=answer,
            steps=steps,
            messages=messages,
            stopped_reason=reason,
            llm_calls=len(steps),
            tool_calls=sum(len(step.results) for step in steps),
            context_tokens=count_message_tokens(messages),
        )
