"""原型 01：ReAct / Agentic Loop。对应笔记第 9 章。

运行方式（PyCharm 里直接右键 Run / Debug 这个文件就行）：
    python experiments/experiment_5_agent_notes_lab/p01_react_loop/main.py

默认走 mock 模型，不联网、结果可复现。想看真实模型的表现：
    set AGENT_NOTES_LLM=real & set DEEPSEEK_API_KEY=sk-xxx

建议打断点的位置：
  - react_loop.py 的 `response = self.llm.chat(messages, tools)`：看模型这一回合拿到了什么上下文
  - react_loop.py 的 `result = self.registry.call(...)`：看框架怎么执行工具
  - react_loop.py 的 `messages.append(result.to_message(...))`：看 Observation 怎么回注历史
"""
from __future__ import annotations

import pathlib
import sys

# 把实验区根目录塞进 sys.path，这样 p01 里能 import common。
# PyCharm 默认只把脚本所在目录加进来，所以这两行必须有。
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from common import trace  # noqa: E402
from common.llm import LLMResponse, Message, MockLLM, ToolCall  # noqa: E402
from common.tools import (  # noqa: E402
    ToolRegistry,
    make_calculator_tool,
    make_search_tool,
)
from react_loop import LoopResult, ReActAgent, Step  # noqa: E402

# --------------------------------------------------------------------------
# 假知识库：让"观察"这一步有真实的东西可回注
# --------------------------------------------------------------------------
CORPUS = {
    "京沪高铁": "北京南到上海虹桥的二等座票价 553 元，车程约 4 小时 28 分。",
    "ReAct": "ReAct = Reasoning + Acting，让模型交替进行推理和行动，再根据行动结果继续推理。",
    "MCP": "MCP 是模型与外部工具之间的标准化协议，采用 Host / Client / Server 三层架构。",
}


def build_registry() -> ToolRegistry:
    registry = ToolRegistry("p01")
    registry.register(make_search_tool(CORPUS))
    registry.register(make_calculator_tool())
    return registry


def count_tool_messages(messages: list[Message]) -> int:
    """数一下已经产生了多少条 Observation。策略函数靠它判断"现在第几回合"。"""
    return sum(1 for m in messages if m.get("role") == "tool")


def last_tool_content(messages: list[Message]) -> str:
    for msg in reversed(messages):
        if msg.get("role") == "tool":
            return str(msg.get("content") or "")
    return ""


def snippets_from_observations(messages: list[Message]) -> str:
    """模拟模型「读懂」观察结果：只把 snippet 那几行挑出来，而不是照抄整段文本。

    真实模型从 Observation 里抽取信息靠的是注意力；这里用字符串处理代替，
    是为了让 mock 的输出也读得下去。
    """
    found: list[str] = []
    for msg in messages:
        if msg.get("role") != "tool":
            continue
        for line in str(msg.get("content") or "").splitlines():
            line = line.strip()
            if line.startswith("snippet:"):
                found.append(line[len("snippet:"):].strip())
    return " ".join(found) if found else last_tool_content(messages).replace("\n", " ")


# --------------------------------------------------------------------------
# 打印：把循环的每一圈摊开给人看
# --------------------------------------------------------------------------


def printer(question: str):
    def on_step(step: Step) -> None:
        trace.step(step.index, f"模型回合 {step.index}（Reason）")
        if step.response.tool_calls:
            trace.note("模型判断：信息不够，需要调用工具（Act）")
            trace.show_tool_calls(step.response.tool_calls)
            for call, result in zip(step.response.tool_calls, step.results):
                print()
                trace.kv("Observation", f"{call.name} -> ok={result.ok}")
                body = result.content or str(result.error or result.protocol_error)
                for line in body.splitlines():
                    print(f"      {line}")
        else:
            trace.note("模型判断：任务完成，输出普通文本（循环结束）")
            trace.kv("最终答案", step.response.content)

    return on_step


def run_scenario(title: str, subtitle: str, question: str, policy, max_steps: int = 6) -> LoopResult:
    trace.section(title, subtitle)
    trace.kv("用户输入", question)
    registry = build_registry()
    trace.show_tools(registry.schemas())

    agent = ReActAgent(
        llm=MockLLM(policy, name="mock-llm"),
        registry=registry,
        max_steps=max_steps,
        on_step=printer(question),
    )
    result = agent.run(question)

    print()
    trace.rule("-")
    trace.kv("循环停止原因", result.stopped_reason)
    trace.kv("模型被调用次数", result.llm_calls)
    trace.kv("工具被调用次数", result.tool_calls)
    trace.kv("最终上下文 token（估算）", result.context_tokens)
    return result


# --------------------------------------------------------------------------
# 五个场景，按"循环转几圈"从少到多排列
# --------------------------------------------------------------------------


def scenario_a_direct_answer() -> LoopResult:
    """模型认为不需要工具，一轮就结束——证明循环的退出条件是什么。"""

    def policy(messages, tools):
        return "MCP 是一种标准化协议，用来统一模型与外部工具的连接方式。"

    return run_scenario(
        "场景 A：不需要工具，循环只转一圈",
        "对应伪代码里 while 的第一次判断就直接走 else 分支",
        "用一句话解释 MCP 是什么。",
        policy,
    )


def scenario_b_single_tool() -> LoopResult:
    """一次工具调用：Reason -> Act -> Observation -> Reason -> 答案。"""

    def policy(messages, tools):
        if count_tool_messages(messages) == 0:
            return ToolCall("search_knowledge", {"query": "ReAct"})
        return "根据检索结果：ReAct 是 Reasoning（推理）和 Acting（行动）的结合。" + snippets_from_observations(messages)

    return run_scenario(
        "场景 B：一次工具调用",
        "Reason -> Act -> Observation -> Reason -> 答案，这是 ReAct 的最小完整形态",
        "ReAct 是什么？",
        policy,
    )


def scenario_c_multi_step() -> LoopResult:
    """多步：第二步的参数依赖第一步的观察结果，这就是"根据行动结果继续判断下一步"。"""

    def policy(messages, tools):
        done = count_tool_messages(messages)
        if done == 0:
            return ToolCall("search_knowledge", {"query": "京沪高铁"})
        if done == 1:
            # 模型从 Observation 里读出了 553，才决定要算 553 * 2 * 2
            return ToolCall("calculator", {"expression": "553*2*2"})
        return "两人往返京沪高铁共 2212 元（553 元 × 2 人 × 2 程）。"

    return run_scenario(
        "场景 C：多步循环（后一步依赖前一步的观察）",
        "这一步是 ReAct 和「一次性生成」的分水岭：第二步的参数来自第一步的真实结果",
        "北京到上海的高铁票价是 553 元，两个人往返一共多少钱？",
        policy,
    )


def scenario_d_parallel_tools() -> LoopResult:
    """模型一次吐出多个 tool_call。框架必须遍历执行，再一次性回注。"""

    def policy(messages, tools):
        if count_tool_messages(messages) == 0:
            return [
                ToolCall("search_knowledge", {"query": "京沪高铁"}),
                ToolCall("search_knowledge", {"query": "MCP"}),
            ]
        return "两条都查到了：" + snippets_from_observations(messages)

    return run_scenario(
        "场景 D：一次回合发出多个工具调用",
        "Harness 要能处理「一轮多个 Act」，并把多条 Observation 一起回注",
        "同时帮我查一下京沪高铁和 MCP。",
        policy,
    )


def scenario_e_bounded_loop() -> LoopResult:
    """模型停不下来时，框架必须兜底。这正是笔记 13.1.4 说的"心跳机制"。"""
    trace.section(
        "场景 E：有界循环（模型停不下来）",
        "对应笔记 13.1.4：最大迭代次数 + 无进展检测，两者都是「心跳机制」的零件",
    )

    def loop_forever(messages, tools):
        # 每次换一个参数，所以不会被"重复调用"检测拦下，只能靠 max_steps 兜底
        return ToolCall("calculator", {"expression": f"1+{count_tool_messages(messages) + 1}"})

    def repeat_same(messages, tools):
        return ToolCall("calculator", {"expression": "1+1"})

    for label, policy, note in [
        ("E-1 每次都换参数，停不下来", loop_forever, "靠 max_steps 兜底"),
        ("E-2 反复调用同一个工具同一组参数", repeat_same, "靠无进展检测提前止损"),
    ]:
        print()
        trace.section(label, note)
        registry = build_registry()
        agent = ReActAgent(
            llm=MockLLM(policy),
            registry=registry,
            max_steps=4,
            on_step=printer(""),
        )
        result = agent.run("帮我算个数。")
        print()
        trace.kv("循环停止原因", result.stopped_reason)
        trace.kv("模型被调用次数", result.llm_calls)
        trace.note(
            "没有这两个兜底，这个循环会一直转下去 —— 笔记里那句话叫"
            '"一台非常自信的烧钱炉"'
        )
    return result


def main() -> None:
    trace.section(
        "原型 01：ReAct / Agentic Loop",
        "笔记 07-Agent开发框架与工程实践.md 第 9 章",
    )
    trace.note("Reason -> Act -> Observation -> Reason -> ... -> 完成")
    trace.note("模型负责推理和选工具，框架负责执行工具、回注结果、判断何时停")

    results = [
        scenario_a_direct_answer(),
        scenario_b_single_tool(),
        scenario_c_multi_step(),
        scenario_d_parallel_tools(),
        scenario_e_bounded_loop(),
    ]

    trace.section("五个场景对照表")
    trace.summary_table(
        [
            {
                "场景": name,
                "停止原因": r.stopped_reason,
                "模型调用": r.llm_calls,
                "工具调用": r.tool_calls,
                "上下文token": r.context_tokens,
            }
            for name, r in zip(
                ["A 直接回答", "B 单次工具", "C 多步", "D 并行工具", "E 有界循环"],
                results,
            )
        ],
        ["场景", "停止原因", "模型调用", "工具调用", "上下文token"],
    )
    trace.note("注意上下文 token 是随轮次单调增长的 —— 这就是后面记忆原型要解决的事")


if __name__ == "__main__":
    main()
