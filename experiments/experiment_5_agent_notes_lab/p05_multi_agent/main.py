"""原型 05：Multi-Agent 架构与通信。对应笔记第 6 章。

这一章的结论反直觉，也是它值得动手做的原因：

    **多 Agent 不一定比单 Agent 好。**

笔记引 Google《Towards a Science of Scaling Agent Systems》：在强顺序、长推理链、
单体基线已经不错（约 45%）、工具密集且预算固定的场景下，盲目上多 Agent 会掉性能。
论文用四个可量化指标横评架构：LLM 调用次数、顺序深度、通信开销、错误放大。

这个原型把四个指标都算出来，让"某架构更好"这句话变成一张带数字的表。

运行方式（PyCharm 直接右键 Run / Debug）：
    python experiments/experiment_5_agent_notes_lab/p05_multi_agent/main.py

建议打断点的位置：
  - architectures.py 的 `run_centralized` 里 `if not ok and rng.random() < gate_catch_rate:`
    → 中心化压制错误放大的那一行就在这
  - architectures.py 的 `amplification=error_rate / per_agent`
    → "错误放大"的定义
  - communication.py 的 `delegate()` 里 `orchestrator.messages.append(tool_result)`
    → 委派模式下，回到主管上下文的只有这一行结果
"""
from __future__ import annotations

import pathlib
import random
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from common import trace  # noqa: E402
from common.llm import count_message_tokens, estimate_tokens  # noqa: E402
from architectures import (  # noqa: E402
    AgentSpec,
    run_centralized,
    run_decentralized,
    run_hybrid,
    run_independent_synthesis,
    run_independent_vote,
    run_sas,
    simulate,
)
from communication import (  # noqa: E402
    AgentContext,
    FileMailbox,
    collaborative_round,
    delegate,
    handoff,
    subagent_as_tool,
)

# 三个子 Agent，各自有 25% 的出错概率。
# 25% 是刻意调高的：真实系统里子 Agent 错误率低得多，
# 但错误放大的机制在低错误率下一样成立，只是数字小、不容易看出来。
PER_AGENT_ERROR = 0.25


def agents() -> list[AgentSpec]:
    return [
        AgentSpec("官网信息 Agent", PER_AGENT_ERROR, role="收集官网信息"),
        AgentSpec("市场监管 Agent", PER_AGENT_ERROR, role="收集监管信息"),
        AgentSpec("友商对比 Agent", PER_AGENT_ERROR, role="收集友商信息"),
    ]


# ==========================================================================
# 演示 1：五种架构的四个指标
# ==========================================================================


def demo_1_architecture_comparison() -> None:
    trace.section(
        "演示 1：五种架构横评（每种子 Agent 错误率固定 25%，各跑 4000 次）",
        "笔记 6.1：LLM 调用次数 / 顺序深度 / 通信开销 / 错误放大",
    )
    crew = agents()
    trace.kv("子 Agent 数量", len(crew))
    trace.kv("单个子 Agent 错误率", f"{PER_AGENT_ERROR:.0%}")

    stats = [
        simulate(run_sas, [crew[0]]),
        simulate(run_independent_synthesis, crew),
        simulate(run_independent_vote, crew),
        simulate(run_centralized, crew),
        simulate(run_decentralized, crew),
        simulate(run_hybrid, crew),
    ]

    print()
    trace.summary_table(
        [stat.as_row() for stat in stats],
        ["架构", "最终错误率", "错误放大", "平均LLM调用", "通信消息数", "平均token", "顺序深度"],
    )

    print()
    trace.note("「错误放大」= 最终错误率 / 单个子 Agent 的错误率。")
    trace.note("  大于 1 = 这个架构在放大错误；小于 1 = 这个架构在吸收错误。")
    print()
    for stat in stats:
        verdict = "放大" if stat.amplification > 1 else "吸收"
        trace.bullet(f"{stat.name:<22} {stat.amplification:>5.2f}x  {verdict}错误   （{stat.detail}）")


# ==========================================================================
# 演示 2：两种"独立多体"完全不是一回事
# ==========================================================================


def demo_2_two_kinds_of_independent() -> None:
    trace.section(
        "演示 2：同样是「独立多体」，分工拼装和投票的结论完全相反",
        "这是面试里最容易混的一个点：Independent 是并行做同一件事，还是各做一块",
    )
    crew = agents()

    synthesis = simulate(run_independent_synthesis, crew)
    vote = simulate(run_independent_vote, crew)

    print()
    trace.summary_table(
        [synthesis.as_row(), vote.as_row()],
        ["架构", "最终错误率", "错误放大", "平均LLM调用", "通信消息数", "平均token", "顺序深度"],
    )
    print()
    trace.kv("分工拼装", synthesis.detail)
    trace.note("  每个子任务独立完成，最后拼起来。**任意一块错了，整体就错了** ——")
    trace.note("  错误在这里是被叠加的，这就是论文里错误放大最严重的那种拓扑。")
    print()
    trace.kv("同任务投票", vote.detail)
    trace.note("  三个 Agent 做同一件事，多数决。物理隔离保证了推理独立性，")
    trace.note("  个体幻觉被投票抵消掉 —— 错误率是被**压下去**的。")
    print()
    trace.note("一个用并行换覆盖广度，一个用并行换抗幻觉能力。名字都叫 Independent，")
    trace.note("但选错了就是完全相反的效果。")


# ==========================================================================
# 演示 3：一次中心化运行的完整轨迹
# ==========================================================================


def demo_3_centralized_trace() -> None:
    trace.section(
        "演示 3：中心化架构跑一次的完整轨迹",
        "笔记 6.5.2：主管分解 -> 并行下发 -> 验收 -> 汇总裁决",
    )
    rng = random.Random(7)  # 固定种子，每次运行的轨迹都一样
    crew = agents()

    trace.kv("用户任务", "分析某上市公司的估值")
    print()
    trace.kv("主管", "分解任务，设计并行窗口，下发子任务")
    for agent in crew:
        trace.bullet(f"派给 {agent.name}：{agent.role}")

    print()
    trace.rule("-")
    print("  验收环节（这一环是中心化压制错误放大的关键）")
    trace.rule("-")
    for agent in crew:
        first = rng.random() >= agent.error_rate
        status = "合格" if first else "不合格"
        line = f"    {agent.name:<16} 第一轮 {status}"
        if not first and rng.random() < 0.8:
            second = rng.random() >= agent.error_rate
            line += f" -> 主管打回返工 -> {'合格' if second else '仍不合格'}"
        print(line)

    print()
    trace.note("子 Agent 的错误有 80% 的概率在验收环节被抓出来并要求返工。")
    trace.note("对照演示 1 的数字看这一环值多少钱：")
    trace.note("  分工拼装（无门控）2.26x  ->  中心化（有门控）1.07x")
    trace.note("拓扑一样（都要求每个子任务都对），差别只在主管有没有验收。")
    trace.note("")
    trace.note("但要注意：中心化并没有把错误率压到 0。原因是最终答案仍然要求")
    trace.note("**每个子任务都对**，子任务越多，这个连乘就越吃亏 ——")
    trace.note("这也是为什么主管要控制并行窗口的规模，而不是把任务切得越碎越好。")
    trace.note("")
    trace.note("代价还有两处：LLM 调用变成 O(R*N)，以及**主管本身可能成为瓶颈**。")
    trace.note("所以笔记提醒：要把编排轮数 R 控制在合理范围。")


# ==========================================================================
# 演示 4：三种通信模式
# ==========================================================================


def demo_4_communication_modes() -> None:
    trace.section(
        "演示 4：三种通信模式 —— 控制权在哪里不一样",
        "笔记 6.4：移交 Handoff / 工具调用 ToolCall / 消息协作",
    )

    # ---- 移交 ----
    print()
    trace.section("4-a 移交 Handoff：控制权完整转移，交棒方退出", "接力跑把棒交出去")
    triage = AgentContext(
        name="分诊 Agent",
        system_prompt="判断用户诉求属于哪一类，然后转给对应的专家。",
        messages=[
            {"role": "user", "content": "我要退订单一 T20260920-0001，顺便问一下发票的事。"},
            {"role": "assistant", "content": "这属于退款业务，转给退款专员。"},
        ],
    )
    result = handoff(triage, "退款专员", "处理退款。用户已确认要退订。")
    trace.kv("移交给", result.to_agent)
    trace.kv("带走的上下文", f"{result.carried_messages} 条消息，{result.context.tokens()} token")
    trace.kv("原 Agent", result.note)
    trace.note("移交通常要连上下文一起交出去，否则接棒方得从头问一遍。")

    # ---- 委派 ----
    print()
    trace.section("4-b 工具调用 ToolCall：主管保留控制权，只收回最终结果", "Claude Code 里 SubAgent 的做法")
    orchestrator = AgentContext(
        name="主管 Agent",
        system_prompt="拆解任务，委派给子 Agent，自己负责最终输出。",
        messages=[
            {"role": "user", "content": "帮我评估 T20260920-0001 这笔订单该不该退款。"},
            {"role": "assistant", "content": "我需要先查订单，再核对退款政策。"},
        ],
    )
    before = orchestrator.tokens()
    sub_messages = [
        {"role": "user", "content": "查订单 T20260920-0001 的退款资格"},
        {"role": "assistant", "content": "先查订单状态……再查会员等级……再查退票规则……"},
        {"role": "tool", "content": "订单 已出票；会员 白金卡；规则 退票免手续费。"},
        {"role": "assistant", "content": "核对完成。"},
    ]
    delegation = delegate(
        orchestrator,
        sub_agent="退款核查 Agent",
        system_prompt="核对订单是否符合退款条件。",
        task="查订单 T20260920-0001 的退款资格",
        sub_messages=sub_messages,
        final_result="订单 T20260920-0001 符合退款条件：白金卡会员，退票免手续费。",
    )
    after = orchestrator.tokens()
    trace.kv("子 Agent 折腾了", f"{len(sub_messages)} 条消息，{delegation.sub_context_tokens} token")
    trace.kv("回到主管上下文的", f"只有 1 条 tool_result，{delegation.returned_tokens} token")
    trace.kv("主管上下文变化", f"{before} -> {after} token")
    trace.note("子 Agent 在私有空间里折腾多久都行，主管上下文只涨了一行 ——")
    trace.note("这是控制上下文规模最有效的一招，也是委派和移交最大的区别。")

    print()
    tool = subagent_as_tool(
        "退款核查",
        "把退款核查交给子 Agent，只返回最终结论，不返回中间过程。",
        run=lambda task: "符合退款条件",
    )
    trace.kv("落地形式", "子 Agent 被包装成一个普通工具")
    trace.kv("工具名", tool.schema()["function"]["name"])
    trace.kv("工具描述", tool.schema()["function"]["description"])
    trace.note("对主管来说，它和 search、calculator 没有任何区别 ——")
    trace.note("主循环不需要知道对面是函数还是一个完整的 Agent。")

    # ---- 消息协作 ----
    print()
    trace.section("4-c 消息协作：长期驻留的 Agent 持续交换消息", "Claude Code 里 AgentTeam 的做法")
    mailbox = FileMailbox()
    mailbox.reset()
    crew = {
        "价格 Agent": AgentContext("价格 Agent", "盯价格"),
        "政策 Agent": AgentContext("政策 Agent", "盯政策"),
        "汇总 Agent": AgentContext("汇总 Agent", "汇总两者"),
    }
    for round_index in (1, 2):
        delivered = collaborative_round(crew, mailbox, round_index)
        trace.kv(f"第 {round_index} 轮投递", delivered)
    print()
    trace.kv("邮箱文件", [p.name for p in sorted(mailbox.root.glob("*.jsonl"))])
    trace.kv("汇总 Agent 收到的消息", len(mailbox.receive("汇总 Agent")))
    for record in mailbox.receive("汇总 Agent"):
        print(f"      [{record['topic']}] 来自 {record['from']}：{record['body']}")
    trace.note("消息量按 O(N^2) 涨 —— 3 个 Agent 两轮就是 12 条。")
    trace.note("笔记提醒：这种拓扑通常要加通信限流/白名单，避免消息爆炸。")
    print()
    trace.note("介质也是设计的一部分：这里用的是**文件邮箱**，消息落盘等着，")
    trace.note("接收方不在线也不会丢；委派那种用的是**内存通道**，同步返回。")


# ==========================================================================
# 演示 5：共享完整推理 vs 只共享最终结果
# ==========================================================================


def demo_5_shared_state_cost() -> None:
    trace.section(
        "演示 5：传什么 —— 共享完整推理数据 vs 只共享最终结果",
        "笔记 6.4：共享完整中间状态便于协作，但会迅速扩大上下文",
    )
    orchestrator = AgentContext("主管 Agent", "拆解并汇总", [{"role": "user", "content": "评估这笔订单"}])

    # 子 Agent 的完整内部过程
    full_reasoning = [
        {"role": "assistant", "content": "我先把问题拆成三步：查订单状态、查会员等级、查退票规则。"},
        {"role": "assistant", "content": "第一步：订单状态是已出票，说明还没使用，可以退。"},
        {"role": "assistant", "content": "第二步：会员等级是白金卡，退票免手续费。"},
        {"role": "assistant", "content": "第三步：规则要求开车前 24 小时以上，现在满足。"},
        {"role": "assistant", "content": "综合判断：符合退款条件。"},
    ]
    final_only = "订单 T20260920-0001 符合退款条件（白金卡，退票免手续费）。"

    full_tokens = count_message_tokens(full_reasoning)
    final_tokens = estimate_tokens(final_only)

    print()
    trace.kv("子 Agent 的完整推理", f"{len(full_reasoning)} 条消息，{full_tokens} token")
    for message in full_reasoning:
        print(f"      - {message['content']}")
    print()
    trace.kv("只共享最终结果", f"1 条消息，{final_tokens} token")

    print()
    trace.summary_table(
        [
            {"做法": "共享完整推理数据", "token": full_tokens, "相对开销": "100%",
             "好处": "其他 Agent 能理解推理过程，协作与纠错更强",
             "代价": "状态空间快速膨胀，上下文和内存管理吃紧"},
            {"做法": "只共享最终结果", "token": final_tokens,
             "相对开销": f"{final_tokens / full_tokens:.0%}",
             "好处": "上下文规模可控",
             "代价": "必须为每个 Agent 定义清晰的结果模式，否则下游看不懂"},
        ],
        ["做法", "token", "相对开销", "好处", "代价"],
    )
    trace.note("笔记的原话：实际系统通常还会通过工具调用、内存通道或文件邮箱")
    trace.note("传递**结构化结果** —— 也就是在这两者之间找一个明确的契约。")


# ==========================================================================
# 演示 6：选型对照
# ==========================================================================


def demo_6_selection_guide() -> None:
    trace.section("演示 6：什么场景选什么架构（笔记 6.2 的结论）")

    print()
    trace.note("先判断该不该上多 Agent。命中下面任意一条，就该留在单 Agent：")
    trace.bullet("强顺序、长推理链 —— 一步错步步错，多 Agent 只会多几个出错点")
    trace.bullet("单体基线已经不错（论文给的阈值约 45%）")
    trace.bullet("工具密集且预算固定 —— 引入多 Agent 会多出一笔「协调税」，吞噬有效 token")

    print()
    trace.summary_table(
        [
            {"任务类型": "顺序推理 / 工具密集", "推荐架构": "单 Agent",
             "原因": "引入多 Agent 反而带来性能下降"},
            {"任务类型": "需要消除幻觉的并行任务", "推荐架构": "独立型（同任务投票）",
             "原因": "物理隔离保证推理独立性，集体投票对抗个体幻觉"},
            {"任务类型": "边界清晰、SOP 明确", "推荐架构": "中心化",
             "原因": "多个 Agent 只在特定框架内填空，由主脑协调汇总"},
            {"任务类型": "高熵搜索、需开放探索", "推荐架构": "去中心化",
             "原因": "外部环境变化快，各 Agent 自主调整，无需中央指令"},
            {"任务类型": "层级控制 + 有限对等通信", "推荐架构": "混合型",
             "原因": "既要有权威收敛，又要一点横向纠错"},
        ],
        ["任务类型", "推荐架构", "原因"],
    )
    trace.note("面试时别停在「什么场景用什么架构」，要能说出**为什么** ——")
    trace.note("比如「顺序调用为什么用单 Agent」「选错架构怎么会放大误差」，")
    trace.note("这些答案都在上面这张表和演示 1 的数字里。")


def main() -> None:
    trace.section(
        "原型 05：Multi-Agent 架构与通信",
        "笔记第 6 章（含 Google《Towards a Science of Scaling Agent Systems》的结论）",
    )
    trace.note("核心结论：多 Agent 不一定比单 Agent 好，选错架构会显著放大误差。")

    demo_1_architecture_comparison()
    demo_2_two_kinds_of_independent()
    demo_3_centralized_trace()
    demo_4_communication_modes()
    demo_5_shared_state_cost()
    demo_6_selection_guide()


if __name__ == "__main__":
    main()
