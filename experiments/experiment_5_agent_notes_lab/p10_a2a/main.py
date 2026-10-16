"""原型 10：A2A（Agent-to-Agent）协议。对应笔记第 8 章。

笔记第 8 章很短，只有一页，但它自己说明了原因：

    （根据最新面试情况，A2A 协议的概念，A2A 考的没有 MCP 那么多，
      没有那么热，但是还是偶尔会问到。暂时问的不深，我们先记一下概念）

所以这个原型的重点不是把 A2A 讲得多深，而是**把它和 MCP 的区别讲清楚**。
笔记第 8 章列的四条特点，每一条都能和 MCP 对照出一个具体的差别：

    1 互操作性          两边都有，但标准化的东西不同
    2 Agent 卡片        **A2A 独有** —— MCP 没有「对面是谁」这个问题
    3 长时协作与异步交互  **A2A 独有** —— MCP 的调用没有中间状态
    4 内部实现不透明      两边都有，但 A2A 是刻意设计的

一句话概括两者的分工：

    **MCP 让你调用工具，A2A 让你委派工作。**
    工具是确定性的、秒回的、幂等的；
    工作是有状态的、要等的、重试会重复执行的。

运行方式（PyCharm 直接右键 Run / Debug）：
    python experiments/experiment_5_agent_notes_lab/p10_a2a/main.py

建议打断点的位置：
  - protocol.py 的 `ALLOWED_TRANSITIONS`：**A2A 和一次 RPC 调用的本质区别就在这张表**
  - protocol.py 的 `Task.transition()`：非法迁移是怎么被拒绝的
  - server.py 的 `TravelAgent._ask_preference()`：`input-required` 是怎么产生的
  - client.py 的 `A2AClient.wait()`：异步在客户端这一侧长什么样
  - server.py 的 `TravelAgent._mcp()`：**A2A Agent 内部用的是 MCP**
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from common import trace  # noqa: E402
from client import A2AClient, TaskUpdate  # noqa: E402
from protocol import (  # noqa: E402
    COMPLETED,
    INPUT_REQUIRED,
    STATE_LABELS,
    TERMINAL_STATES,
    AgentCard,
    StateTransitionError,
    Task,
)
from server import AgentDirectory, ExpenseAgent, TravelAgent  # noqa: E402


def build_environment() -> tuple[AgentDirectory, A2AClient, AgentCard, AgentCard]:
    directory = AgentDirectory()
    travel_card = directory.register(TravelAgent())
    expense_card = directory.register(ExpenseAgent())
    return directory, A2AClient(directory, auth_token="demo-token"), travel_card, expense_card


# ==========================================================================
# 演示 1：Agent 卡片 —— A2A 独有的「发现」
# ==========================================================================


def demo_1_agent_card(client: A2AClient, travel: AgentCard, expense: AgentCard) -> None:
    trace.section(
        "演示 1：Agent 卡片 —— A2A 独有的「发现」这一步",
        "笔记 8.2：每个 Agent 通过「卡片」描述自己的能力、接口、身份和安全策略",
    )
    trace.note("笔记那句话说卡片要描述四样东西，卡片上的字段就是按这四样分的：")
    print()
    trace.summary_table(
        [
            {"要描述的": "能力", "卡片上的字段": "skills", "例子": travel.skills[0].name},
            {"要描述的": "接口", "卡片上的字段": "url / protocolVersion", "例子": travel.url},
            {"要描述的": "身份", "卡片上的字段": "provider / name / version", "例子": f"{travel.provider} v{travel.version}"},
            {"要描述的": "安全策略", "卡片上的字段": "securitySchemes / security", "例子": travel.describe_security()},
        ],
        ["要描述的", "卡片上的字段", "例子"],
    )

    print()
    trace.section("目录里有两个 Agent")
    trace.summary_table(
        [
            {
                "Agent": card.name,
                "服务方": card.provider,
                "技能": "、".join(s.name for s in card.skills),
                "鉴权": card.describe_security(),
            }
            for card in (travel, expense)
        ],
        ["Agent", "服务方", "技能", "鉴权"],
    )

    print()
    for card in (travel, expense):
        trace.kv(f"{card.name} 声明支持", card.describe_capabilities())
    trace.note("注意两个 Agent 声明的能力不一样——**这是能被程序读的**，")
    trace.note("调用方可以据此决定要不要用流式，而不是去翻文档。")

    print()
    trace.section("Host 拿着一个请求找候选")
    request = "帮我把订单 T20260920-0001 改签到明天上午"
    trace.kv("请求", request)
    hits = client.route(request)
    print()
    trace.summary_table(
        [{"命中的 Agent": card.name, "命中的技能": skill.name, "服务方": card.provider}
         for card, skill in hits],
        ["命中的 Agent", "命中的技能", "服务方"],
    )

    print()
    trace.note("**这一步在 MCP 里根本不存在。**")
    trace.note("MCP 的 Server 是你自己配置好的——写代码的时候你就知道要连哪个知识库、")
    trace.note("哪个订单系统。`tools/list` 只是问「你这儿有哪些工具」，")
    trace.note("它不解决「我该连谁」这个问题。")
    trace.note("")
    trace.note("而 A2A 的 Agent 是**运行时才知道的**，可能来自别的团队、别的公司。")
    trace.note("于是「该派给谁」变成了一个真问题，卡片就是为它准备的。")
    print()
    trace.note("还要注意卡片上**没有**什么：没有「我用什么模型」、")
    trace.note("没有「我有哪些工具」、没有提示词。这不是遗漏，是设计目标——")
    trace.note("就是笔记第 8 章第 4 条说的「内部实现不透明」。演示 4 会验证这一点。")


# ==========================================================================
# 演示 2：一个长时任务的完整生命周期
# ==========================================================================


def demo_2_lifecycle(client: A2AClient, travel: AgentCard) -> None:
    trace.section(
        "演示 2：一个长时任务的完整生命周期",
        "笔记 8.3：支持多轮对话、任务委派、进度更新、流式响应",
    )
    trace.note("派一个改签任务出去。注意下面三件事同时发生：")
    trace.note("  1. 状态在迁移（submitted → working → input-required → working → completed）")
    trace.note("  2. 进度消息在往上冒（那是「进度更新」）")
    trace.note("  3. **它中途停下来问了一个问题**，等我们回答才接着跑")

    transitions: list[tuple[str, str]] = []

    def on_input(task: Task) -> str:
        transitions.append((task.state, "（客户端送回答）"))
        # 注意只看 text 那个 part。这条消息还带了一个 data part（候选车次列表），
        # 把整个 message.text 打出来会是一大坨 JSON。
        question = next(p.render() for p in task.history[-1].parts if p.kind == "text")
        trace.note(f"-> 对方在等我们回答：{question}")
        return "G103 08:30 那趟"

    task = client.send_task(travel, "帮我把 T20260920-0001 改签到明天上午")
    trace.kv("派发后立刻拿到", f"task_id = {task.id}，状态 = {STATE_LABELS[task.state]}")

    result = client.run_task(
        travel,
        "帮我把 T20260920-0001 改签到明天上午",
        on_input_required=on_input,
    )

    print()
    trace.section("任务历史（这就是「过程」，MCP 的返回值里没有这一层）")
    print()
    for index, message in enumerate(result.task.history):
        who = "客户端" if message.role == "user" else "Agent "
        state = ""
        if message.role == "agent":
            state = ""
        text = message.text
        if len(text) > 46:
            text = text[:46] + "…"
        print(f"  [{index:02d}] {who}  {text}")

    print()
    trace.section("进度事件（对端推上来的）")
    for update in result.updates:
        # 有些进度消息里夹着 MCP 返回的原文，截一下才看得清
        text = update.text if len(update.text) <= 46 else update.text[:46] + "…"
        trace.bullet(TaskUpdate(update.task_id, update.state, text, update.is_terminal).render())

    print()
    trace.section("状态迁移")
    trace.summary_table(
        [
            {"阶段": "派发", "状态": "已提交", "说明": "拿到 task_id，对方还没开始干"},
            {"阶段": "推进 1", "状态": "处理中", "说明": "查订单（内部通过 MCP）"},
            {"阶段": "推进 2", "状态": "处理中", "说明": "查会员权益（内部通过 MCP）"},
            {"阶段": "推进 3", "状态": "等待补充输入", "说明": "**停下来问用户选哪趟车**"},
            {"阶段": "客户端回话", "状态": "处理中", "说明": "从原地接着跑，不是重头来"},
            {"阶段": "推进 4", "状态": "处理中", "说明": "算差价"},
            {"阶段": "完成", "状态": "已完成", "说明": "产出 artifact"},
        ],
        ["阶段", "状态", "说明"],
    )

    print()
    trace.section("产物（artifact）")
    for artifact in result.task.artifacts:
        trace.kv("名称", artifact.name)
        trace.kv("说明", artifact.description)
        for part in artifact.parts:
            trace.bullet(f"[{part.kind}] {part.render()}")

    print()
    trace.note("**回到 p06 对照一下**：`tools/call` 的返回值里，")
    trace.note("只有最后那个 artifact 的位置上有点东西，前面全都没有——")
    trace.note("没有 task_id、没有中间状态、没有进度消息、没有「等一下我需要你回答」。")
    trace.note("")
    trace.note("不是 MCP 设计得差，是**工具调用本来就不需要这些**。")
    trace.note("你不会希望一次读数据库的操作中途停下来问你问题。")
    trace.note("但如果对面是个 Agent，它中途问你「您想坐哪趟车」就是完全正常的。")


# ==========================================================================
# 演示 3：同一件事，MCP 风格 vs A2A 风格
# ==========================================================================


def demo_3_side_by_side(client: A2AClient, travel: AgentCard) -> None:
    trace.section(
        "演示 3：同一件事，两种协议的写法差多少",
        "这是整个原型最想让你看清的一幕",
    )
    trace.note("先看 MCP 风格——一行调用，立刻拿到结果：")
    print()
    text = client.call_tool_directly("query_knowledge", collection="travel_policy",
                                     question="改签手续费", top_k=1)
    trace.kv("call_tool(...)", " ".join(text.split())[:60])
    trace.note("拿到了就是拿到了，没有第二种可能。")

    print()
    trace.note("再看 A2A 风格——同一件事，但要分好几步：")
    print()
    task = client.send_task(travel, "帮我把 T20260920-0001 改签到明天上午")
    trace.bullet(f"1. send_task(...)      -> 拿到 {task.id}，状态「{STATE_LABELS[task.state]}」")
    result = client.wait(task)
    trace.bullet(f"2. wait(task)          -> 拿到进度事件 {len(result.updates)} 条")
    trace.bullet(f"3. 状态 = {STATE_LABELS[result.task.state]}，原因 = {result.reason}")
    while result.needs_input:
        client.send_message(result.task, "G103 08:30")
        trace.bullet("4. send_message(...)   -> 补充输入，从原地接着跑")
        result = client.wait(result.task)
        trace.bullet(f"5. wait(task)          -> 状态 = {STATE_LABELS[result.task.state]}")
    trace.bullet(f"6. artifacts           -> {[a.name for a in result.task.artifacts]}")

    print()
    trace.summary_table(
        [
            {"对比项": "对端是什么", "MCP（p06）": "工具 / 资源", "A2A（本原型）": "另一个 Agent"},
            {"对比项": "对端的状态", "MCP（p06）": "无状态", "A2A（本原型）": "有状态（一个任务跑很久）"},
            {"对比项": "调用语义", "MCP（p06）": "call → 立刻返回结果", "A2A（本原型）": "send → 拿到 id，之后慢慢等"},
            {"对比项": "中间状态", "MCP（p06）": "没有（成功 / 失败）", "A2A（本原型）": "8 个状态，含「等补充输入」"},
            {"对比项": "时间量级", "MCP（p06）": "毫秒 ~ 秒", "A2A（本原型）": "分钟 ~ 天"},
            {"对比项": "能不能中途插话", "MCP（p06）": "不能", "A2A（本原型）": "**能**（input-required）"},
            {"对比项": "重试的后果", "MCP（p06）": "通常安全（工具幂等）", "A2A（本原型）": "**可能重复执行**"},
            {"对比项": "发现问题", "MCP（p06）": "不需要（Server 是配好的）", "A2A（本原型）": "agent card + 路由"},
            {"对比项": "返回值", "MCP（p06）": "一份数据", "A2A（本原型）": "过程 + 进度 + 产物"},
            {"对比项": "客户端要写多少", "MCP（p06）": "一行", "A2A（本原型）": "一个循环"},
        ],
        ["对比项", "MCP（p06）", "A2A（本原型）"],
    )

    print()
    trace.note("最后一行不是 A2A 的缺点，是**对端在做的事本来就不同**。")
    trace.note("一行代码能搞定的事，说明那件事本身就没有中间过程。")
    trace.note("硬要用一行代码去调一个要跑十分钟的 Agent，你只是把复杂度藏起来了。")


# ==========================================================================
# 演示 4：A2A Agent 内部用的是 MCP
# ==========================================================================


def demo_4_a2a_inside_mcp(travel: AgentCard, directory: AgentDirectory) -> None:
    trace.section(
        "演示 4：A2A Agent 内部用的是 MCP（所以它们是上下楼，不是竞品）",
        "笔记第 8 章说 A2A 让不同 Agent 互操作；那 Agent 自己怎么用工具？还是 MCP",
    )

    agent = directory.resolve(travel)
    trace.note("刚才那个 A2A Agent 内部调了什么？把它的 MCP 调用记录打出来：")
    print()
    mcp_calls = agent.mcp_calls  # type: ignore[attr-defined]
    trace.summary_table(
        [{"第几次": i + 1, "MCP 工具": name} for i, name in enumerate(mcp_calls)],
        ["第几次", "MCP 工具"],
    )

    print()
    trace.note("**这就是两个协议的位置关系：**")
    print()
    print("      Host  ──A2A──>  差旅 Agent  ──MCP──>  知识库 / 订单系统")
    print("            （委派工作）              （调用工具）")
    print()
    trace.note("A2A 在外层：Agent 之间互相委派工作。")
    trace.note("MCP 在内层：一个 Agent 去够到它自己的能力。")
    print()
    trace.note("所以它们的「竞品」说法是站不住的——**它们解决的是两个正交的问题**：")
    trace.note("")
    trace.note("    MCP  解决「Agent 怎么用上工具」——垂直方向")
    trace.note("    A2A  解决「Agent 怎么跟别的 Agent 协作」——水平方向")
    trace.note("")
    trace.note("一个组织里可能同时有几十个 MCP Server 和几个 A2A Agent，")
    trace.note("它们各自在不同的层上工作。")


# ==========================================================================
# 演示 5：内部实现不透明
# ==========================================================================


def demo_5_opacity(directory: AgentDirectory, client: A2AClient,
                   travel: AgentCard, expense: AgentCard) -> None:
    trace.section(
        "演示 5：内部实现不透明 —— 换掉整个 Agent，Host 一行代码不用改",
        "笔记 8.4：调用方只知道对方是一个 Agent，不关心它内部用什么模型或工具",
    )
    trace.note("这两个 Agent 的内部实现差得很远。**下面这三列不是写死的，是量出来的**——")
    trace.note("跑一个任务，同时数它内部发生了什么：")
    print()

    measured: list[dict[str, str]] = []
    for card, text in (
        (travel, "帮我把 T20260920-0001 改签到明天上午"),
        (expense, "审一下这张住宿发票"),
    ):
        agent = directory.resolve(card)
        mcp_before = len(getattr(agent, "mcp_calls", []))
        result = client.run_task(card, text, on_input_required=lambda _task: "G103 08:30")
        mcp_after = len(getattr(agent, "mcp_calls", []))
        state_changes = len([m for m in result.task.history if m.role == "agent"])
        measured.append(
            {
                "Agent": card.name,
                "服务方": card.provider,
                "内部产出的消息": f"{state_changes} 条",
                "内部调 MCP": f"{mcp_after - mcp_before} 次",
                "中途提问": "**会**" if result.updates and any(
                    u.state == INPUT_REQUIRED for u in result.updates) else "不会",
                "对外结果": STATE_LABELS[result.task.state],
            }
        )

    trace.summary_table(measured, ["Agent", "服务方", "内部产出的消息", "内部调 MCP", "中途提问", "对外结果"])

    print()
    trace.note("但 Host 那边用的是**同一段代码**：")
    print()
    print("      task = client.send_task(card, text)")
    print("      result = client.wait(task)")
    print()
    trace.note("换成哪个 Agent，这两行都不变——上面的表格就是证据：")
    trace.note("内部一个调了 2 次 MCP、会停下来问问题，另一个什么都没调、一步干完；")
    trace.note("**但对外它们长得一模一样，Host 的代码一个字都不用改。**")

    print()
    trace.note("调用方不知道也不用知道：对面跑在哪个机房、背后是什么模型、")
    trace.note("内部调了几次工具。")
    trace.note("")
    trace.note("**这就是「内部实现不透明」的价值。**笔记第 8 章的适用场景里写着")
    trace.note("「跨团队或跨厂商的 Agent 协作」「生态互通」——")
    trace.note("如果调用方必须知道对面的内部细节，跨厂商协作就不可能发生。")
    print()
    trace.note("顺便注意一个细节：两个卡片的 capabilities 不一样——")
    trace.note("差旅 Agent 声明了 streaming，报销 Agent 没有。")
    trace.note("调用方可以据此决定要不要用流式。**能力声明在卡片上，而不是在文档里**，")
    trace.note("这就是卡片比一份 API 文档强的地方：它能被程序读。")


# ==========================================================================
# 演示 6：状态机的两条实际后果
# ==========================================================================


def demo_6_state_machine(client: A2AClient, travel: AgentCard, directory: AgentDirectory) -> None:
    trace.section(
        "演示 6：状态机带来的两条实际后果",
        "为什么 A2A 不能像调工具那样简单地重试",
    )

    task = client.send_task(travel, "把 T20260920-0001 改签到明天上午")
    result = client.run_task(travel, "把 T20260920-0001 改签到明天上午",
                             on_input_required=lambda _task: "G103 08:30")

    agent = directory.resolve(travel)
    print()
    trace.section("后果一：终态是吸收态")
    trace.kv("任务当前状态", STATE_LABELS[result.task.state])
    trace.note("这时候再推它一步会怎样？")

    history_before = len(result.task.history)
    agent.step(result.task.id)  # type: ignore[attr-defined]
    trace.kv("再推一步之后", f"状态还是 {STATE_LABELS[result.task.state]}，"
             f"历史条数 {'没变' if len(result.task.history) == history_before else '变了'}")

    print()
    trace.note("这是**对的**：已完成的任务不该被「再推一下」拉回处理中。")
    trace.note("但状态机不只是拦这一下——它还要能拦住非法迁移。看：")
    print()
    try:
        result.task.transition("working")
    except StateTransitionError as exc:
        trace.kv("试图改回「处理中」", "被拒绝")
        trace.note("拒绝理由：")
        trace.note(f"  {exc}")
    trace.note("非法迁移直接抛错，不静默接受——这和 p06 里「协议级错误」是同一个处理哲学。")

    print()
    trace.section("后果二：重试可能把活干两遍")
    trace.note("网络超时的时候，客户端最自然的反应是重发一遍。")
    trace.note("对 MCP 来说这没什么：工具是幂等的，重发只是重读一遍数据。")
    trace.note("对 A2A 来说，**对面会真的再改一次签**。")
    print()

    naive = client.send_task(travel, "把 T20260920-0001 改签到明天上午", message_id="retry-demo")
    retried = client.send_task(travel, "把 T20260920-0001 改签到明天上午", message_id="retry-demo")
    trace.kv("重发同一个 messageId（不去重）",
             f"{naive.id} 和 {retried.id} —— {'两个任务！' if naive.id != retried.id else '同一个任务'}")

    safe = client.send_task(travel, "把 T20260920-0001 改签到明天上午",
                            message_id="retry-demo-2", idempotent=True)
    safe_again = client.send_task(travel, "把 T20260920-0001 改签到明天上午",
                                  message_id="retry-demo-2", idempotent=True)
    trace.kv("重发同一个 messageId（去重）",
             f"{safe.id} 和 {safe_again.id} —— {'两个任务！' if safe.id != safe_again.id else '同一个任务'}")

    print()
    trace.note("所以 A2A 的重试必须带幂等键（messageId 就是这个用途）。")
    trace.note("而 MCP 那边，工具自己声明 `idempotentHint` 就够了——")
    trace.note("**两边都需要幂等，但需要的理由不一样：**")
    trace.note("")
    trace.note("    MCP：重试会重复「读」，浪费但无害（所以只是个 hint）")
    trace.note("    A2A：重试会重复「做」，可能真的改两次签（所以是必须的机制）")
    print()
    trace.note("笔记第 8 章说的「长时协作」，成本就藏在这些地方——")
    trace.note("不是把请求发出去就完了，是发出去之后**还得有办法收尾**。")


def main() -> None:
    trace.section(
        "原型 10：A2A（Agent-to-Agent）协议",
        "笔记第 8 章：互操作性、Agent 卡片、长时异步协作、内部实现不透明",
    )
    trace.note("笔记这一章只有一页，但它自己说明了原因：A2A「考的没有 MCP 那么多」。")
    trace.note("所以这个原型的重点不是把 A2A 讲深，而是**把它和 MCP 的区别讲清楚**。")
    trace.note("")
    trace.note("一句话：**MCP 让你调用工具，A2A 让你委派工作。**")

    directory, client, travel, expense = build_environment()

    demo_1_agent_card(client, travel, expense)
    demo_2_lifecycle(client, travel)
    demo_3_side_by_side(client, travel)
    demo_4_a2a_inside_mcp(travel, directory)
    demo_5_opacity(directory, client, travel, expense)
    demo_6_state_machine(client, travel, directory)

    trace.section("最后收一下")
    trace.summary_table(
        [
            {"问题": "什么时候用 MCP", "判断依据": "对端是「能力」：数据库、API、文件系统",
             "特征": "确定性的、快的、幂等的"},
            {"问题": "什么时候用 A2A", "判断依据": "对端是「角色」：另一个团队/厂商的 Agent",
             "特征": "概率性的、慢的、有状态的"},
            {"问题": "什么时候两个都用", "判断依据": "大多数真实系统", "特征": "A2A 在外层编排，MCP 在内层供能"},
        ],
        ["问题", "判断依据", "特征"],
    )
    print()
    trace.note("判断依据其实很简单：**对面那个东西会不会「自己拿主意」。**")
    trace.note("")
    trace.note("不会 → 它是工具，用 MCP。你会希望它快、准、可重复。")
    trace.note("会   → 它是 Agent，用 A2A。你得接受它慢、会问你问题、可能失败，")
    trace.note("       所以你需要任务、状态、进度、产物这一整套东西来接住它。")
    print()
    trace.note("笔记第 8 章的最后一句话是「生态互通：构建开放的 Agent 网络，")
    trace.note("而不是单一厂商闭环」。要走到那一步，靠的就是这两套协议各管一段。")


if __name__ == "__main__":
    main()
