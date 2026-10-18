"""原型 08：Loop 工程 & Graph 工程。对应笔记第 13 章。

这一章笔记写得很长，但真正的硬核只有两句话：

    13.1.2  cron 是在执行脚本，loop 是在模仿一个工程师。
            差别在于——**决策者在循环里面**。

    13.2.2  Loop 是完成一个任务的反馈闭环；
            Graph 是组织多个任务、角色、分支和反馈闭环的更高一层结构。

这两句话都可以用代码检验。所以这个原型的六个演示，每一个都在回答
「这句话到底是不是真的」：

    演示 1  五个积木 + 一个记事本，拼起来跑一遍
    演示 2  同样的触发器、同样的工具，cron 和 loop 谁能走出去
    演示 3  心跳的三道刹车，各挡一种死法
    演示 4  状态文件：跨会话续跑 vs 每次从零重启
    演示 5  Graph：把多个 loop 编排成一张执行图
    演示 6  控制图：20 个 Agent 为什么会「以工业化规模彼此赞同」

运行方式（PyCharm 直接右键 Run / Debug）：
    python experiments/experiment_5_agent_notes_lab/p08_loop_graph/main.py

建议打断点的位置：
  - loop_engine.py 的 `Worker.decide()`：**loop 和 cron 的全部差别就在这个函数里**
  - loop_engine.py 的 `Heartbeat.check()`：loop 是怎么知道该停的
  - loop_engine.py 的 `Loop.run()`：一轮循环的完整形状
  - loop_engine.py 的 `StateFile.save()`：记事本到底写了什么进磁盘
  - graph.py 的 `Graph.run()`：节点返回值怎么决定下一步（旁路/回环都长在这）
  - graph.py 的 `tally()`：同质组的三个人为什么是「同一双眼睛的三份拷贝」
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from common import trace  # noqa: E402
from common.tools import ToolRegistry, make_search_tool  # noqa: E402
from graph import (  # noqa: E402
    AGGREGATIONS,
    ControlGraph,
    FanOut,
    Graph,
    GraphState,
    RealityAnchor,
    Reviewer,
    make_reviewers,
    run_consensus_trials,
)
from loop_engine import (  # noqa: E402
    Action,
    Connector,
    CronRunner,
    Defect,
    Heartbeat,
    Loop,
    RunState,
    Skill,
    StateFile,
    SubAgent,
    Trigger,
    Verifier,
    Worker,
    Workspace,
)

WORKSPACE_ROOT = pathlib.Path(__file__).resolve().parent / "workspace"


# ==========================================================================
# 共用零件：一个假想项目的 skill、工作区、问题清单
# ==========================================================================


def make_skill() -> Skill:
    """项目手册。它是 loop 自己推不出来的那部分知识。"""
    return Skill(
        name="CLAUDE.md（agent-project）",
        rules=[
            "import 必须按 标准库 / 第三方 / 本项目 三组排列，组间空一行",
            "所有公开函数必须有类型标注",
            "禁止在 handler 里直接 print，统一走 logger",
        ],
        pitfalls=[
            "通用 formatter 只重排格式，不会重排 import 分组——顺序问题得手工改",
            "上次有人把 retry 逻辑写进 handler，导致重试风暴",
        ],
        build_commands=["pytest -q", "ruff check src/", "mypy src/"],
    )


def make_defects(extra: list[Defect] | None = None) -> list[Defect]:
    """当前工作区里的问题。

    三种 strategy 的比例，决定这个 loop 值不值得做：
    全是 generic 的活，写个脚本就够了，不需要 loop。
    """
    defects = [
        Defect("lint-import-order", "lint", "import 没有按团队约定分组", "skill_guided"),
        Defect("test-empty-list", "test", "空列表输入会抛 IndexError", "generic"),
        Defect("review-naming", "review", "变量名 t 表意不清", "generic"),
        Defect("review-typehints", "review", "公开函数缺类型标注", "generic"),
    ]
    return defects + list(extra or [])


def make_state_file(name: str) -> StateFile:
    return StateFile(WORKSPACE_ROOT / name)


def show_history(state: RunState) -> None:
    print()
    trace.summary_table(state.history, ["轮次", "检查", "决定", "结果", "分数"])


def unfixed(defects: list[Defect], limit: int = 2) -> str:
    """表格列里放不下太多，多的用「等 N 个」收尾。"""
    remaining = [d.id for d in defects if not d.fixed]
    if not remaining:
        return "无"
    if len(remaining) <= limit:
        return "、".join(remaining)
    return "、".join(remaining[:limit]) + f" 等 {len(remaining)} 个"


# ==========================================================================
# 演示 1：五个积木 + 一个记事本
# ==========================================================================


def demo_1_five_bricks() -> None:
    trace.section(
        "演示 1：一套完整的循环 = 五个积木 + 一个记事本",
        "笔记 13.1.3：loop 不是「一个会重复的提示词」，它是一套拼起来的装置",
    )

    trigger = Trigger("ci_failure", "PR #142 的 CI 红了")
    workspace = Workspace(branch="loop/142-fix-ci")
    skill = make_skill()
    defects = make_defects()
    subagents = [
        SubAgent("codex", "writer", "模型A", "改代码，让检查全过"),
        SubAgent("reviewer", "reviewer", "模型B", "别管他写了什么，只看 diff 和规格"),
    ]
    connectors = [Connector("知识库 MCP", ToolRegistry().add(
        name="search_knowledge",
        description="在内部知识库里检索",
        parameters={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        handler=lambda query: {"results": [{"title": query, "snippet": "（内部规范库里的相关条目）"}]},
    ))]

    print()
    trace.summary_table(
        [
            {"积木": "一 触发器", "代码里的样子": trigger.describe()},
            {"积木": "二 worktree", "代码里的样子": workspace.describe()},
            {"积木": "三 Skill", "代码里的样子": f"{skill.name}（{len(skill.rules)} 条约定，{skill.token_cost()} token）"},
            {"积木": "四 连接器", "代码里的样子": connectors[0].name},
            {"积木": "五 子 Agent", "代码里的样子": " ＋ ".join(a.describe() for a in subagents)},
            {"积木": "记事本 状态文件", "代码里的样子": make_state_file("demo1-STATE.md").path.name},
        ],
        ["积木", "代码里的样子"],
    )
    trace.note("注意积木五：写和审用的是**不同模型**。这不是为了好看——")
    trace.note("同一个模型审自己写的东西，盲区和写的时候是同一片，等于没审。")

    state_file = make_state_file("demo1-STATE.md")
    if state_file.path.exists():
        state_file.path.unlink()  # 从干净状态开始，好让每次运行的输出一样

    loop = Loop(
        name="ci-fix-loop",
        trigger=trigger,
        workspace=workspace,
        skill=skill,
        verifier=Verifier(defects, review_threshold=4.0),
        defects=defects,
        heartbeat=Heartbeat(max_iterations=8, no_progress_limit=2, budget_usd=2.0),
        state_file=state_file,
        worker=Worker("codex", skill, connectors[0]),
        subagents=subagents,
        connectors=connectors,
    )

    print()
    trace.section("一轮循环的形状")
    for line in [
        "读记事本  →  看当前状态  →  决定下一步  →  执行  →  跑检查  →  写记事本  →  问心跳",
        "",
        "其中「决定下一步」这一步是真的在执行者代码里做的（Worker.decide），",
        "不是一个固定的第一步第二步第三步。这是 loop 和 cron 的全部差别。",
    ]:
        print(f"  {line}" if line else "")

    report = loop.run("让 src/agent.py 通过所有检查")
    show_history(report.state)

    print()
    trace.kv("结果", report.outcome)
    trace.kv("轮数", report.state.iteration)
    trace.kv("花费", f"${report.state.spent_usd:.2f}")
    trace.kv("worktree", workspace.describe())

    print()
    trace.note(f"检查跑了 {report.state.iteration} 轮才全绿，其中 review-typehints 一直没被修。")
    trace.note("为什么还是过了？因为评审是**打分制**：每个未修的问题扣 1 分，")
    trace.note("5 减 1 等于 4，正好卡在门槛上。")
    trace.note("")
    trace.note("这就是打分制的特点——它天然会留一个小尾巴。")
    trace.note("所以关键项要用**通过/失败制**（lint、test 就是），不能用打分制。")
    trace.note("一个「4 分就能上线」的门槛，等于宣布「可以留一个问题不修」。")

    print()
    trace.kv("状态文件", f"{state_file.path}")
    trace.note("打开这个文件看看——loop 的公共记忆就长这样，纯文本，人能读也能改。")


# ==========================================================================
# 演示 2：cron vs loop
# ==========================================================================


def demo_2_cron_vs_loop() -> None:
    trace.section(
        "演示 2：cron 是在执行脚本，loop 是在模仿一个工程师",
        "笔记 13.1.2：这是整章的分水岭，也是「loop 不就是个 cron 吗」的答案",
    )

    results = []
    for label, worker_factory in (("cron（固定脚本）", lambda s: CronRunner()), ("loop（会看状态）", lambda s: Worker("codex", s))):
        skill = make_skill()
        defects = make_defects()
        state_file = make_state_file(f"demo2-{label[:4]}.md")
        if state_file.path.exists():
            state_file.path.unlink()
        loop = Loop(
            name=label,
            trigger=Trigger("ci_failure", "PR #142 的 CI 红了"),
            workspace=Workspace(branch=f"loop/{label[:4]}"),
            skill=skill,
            verifier=Verifier(defects, review_threshold=4.0),
            defects=defects,
            heartbeat=Heartbeat(max_iterations=8, no_progress_limit=2, budget_usd=5.0),
            state_file=state_file,
            worker=worker_factory(skill),
        )
        report = loop.run("让 src/agent.py 通过所有检查")
        results.append((label, report, defects))

    for label, report, defects in results:
        print()
        trace.section(f"{label}：逐轮记录")
        show_history(report.state)

    print()
    trace.section("同一个任务，两种结果")
    trace.summary_table(
        [
            {
                "执行者": label,
                "轮数": report.state.iteration,
                "结果": "全部通过" if report.passed else "没修完",
                "停下原因": report.brake or "目标达成",
                "最后卡在": "、".join(d.id for d in defects if not d.fixed) or "无",
            }
            for label, report, defects in results
        ],
        ["执行者", "轮数", "结果", "停下原因", "最后卡在"],
    )

    print()
    trace.note("cron 每一步都没做错——它跑了 formatter、跑了通用修复、跑了测试，")
    trace.note("而且它把三个「通用做法能修」的问题全修了。")
    trace.note("")
    trace.note("它唯一没做的事是：**看一眼上一轮发生了什么**。")
    trace.note("lint 从第 1 轮就在报同一个错，它每一轮都当没看见，")
    trace.note("因为它的脚本里没有「读一下错误信息」这一步。")
    trace.note("")
    trace.note("loop 多做的也只有一件事：它发现「通用做法修不掉这个」，")
    trace.note("于是去翻了一遍项目手册（积木三），按团队约定改。")
    trace.note("")
    trace.note("这就是笔记那句话的准确含义——差别不在于跑几次，")
    trace.note("而在于**决策者在不在循环里面**。")


# ==========================================================================
# 演示 3：心跳的三道刹车
# ==========================================================================


class StubbornWorker(Worker):
    """一个「死磕型」执行者：它认不出「这个问题需要人决策」。

    它会一遍遍重试同一个修法。这不是夸张——模型在面对
    「改不动的问题」时，最常见的失败模式就是换个说法再试一次。
    """

    def decide(self, state: RunState, defects: list[Defect]) -> Action:
        action = super().decide(state, defects)
        if action.kind == "escalate":
            # 它看不见「该交给人了」这个选项，只会再来一遍
            return Action("patch", f"再试一次 {action.defect}（换个写法说不定就好了）", defect=action.defect)
        return action


def demo_3_heartbeat() -> None:
    trace.section(
        "演示 3：心跳的三道刹车",
        "笔记 13.1.4：每个正经的 loop 都得配最大迭代次数、无进展检测、预算上限",
    )
    trace.note("「预算就是架构的一部分」——先看三道刹车各挡什么死法：")
    print()
    trace.summary_table(
        [
            {"刹车": "budget", "挡的是": "烧钱", "具体挡法": "不管有没有产出都在烧 token"},
            {"刹车": "no_progress", "挡的是": "原地打转", "具体挡法": "分数一动不动，再跑一万轮也一样"},
            {"刹车": "max_iterations", "挡的是": "永远差一点点", "具体挡法": "看着快成了，就是不成"},
        ],
        ["刹车", "挡的是", "具体挡法"],
    )

    # 为了让对照干净，A/B 只用两个问题：一个要读手册，一个机器修不了。
    # 问题一多，执行者会先去修那些能修的，就把「它认不认得出该交给人」这件事淹掉了。
    def two_defects() -> list[Defect]:
        return [
            Defect("lint-import-order", "lint", "import 没有按团队约定分组", "skill_guided"),
            # 注意这个是 test 而不是 review：test 是通过/失败制，漏不掉。
            # 要是挂在 review 上，打分制会给它留 1 分的余地，整个演示就不成立了。
            Defect("arch-sharding", "test", "分库分表方案要重新选型（测试跑不过）", "needs_human"),
        ]

    scenarios = [
        (
            "A 执行者靠得住",
            lambda skill: Worker("codex", skill),
            Heartbeat(max_iterations=10, no_progress_limit=3, budget_usd=5.0),
            two_defects,
        ),
        (
            "B 执行者死磕",
            lambda skill: StubbornWorker("codex", skill),
            Heartbeat(max_iterations=10, no_progress_limit=2, budget_usd=5.0),
            two_defects,
        ),
        (
            "C 钱包先撑不住",
            lambda skill: Worker("codex", skill),
            Heartbeat(max_iterations=10, no_progress_limit=3, budget_usd=0.30, cost_per_iteration=0.15),
            make_defects,
        ),
        (
            "D 活的量太大",
            lambda skill: Worker("codex", skill),
            Heartbeat(max_iterations=3, no_progress_limit=3, budget_usd=5.0),
            lambda: make_defects(
                [Defect(f"review-extra-{i}", "review", f"第 {i} 处需要重构", "generic") for i in range(1, 5)]
            ),
        ),
    ]

    rows = []
    for label, worker_factory, heartbeat, defect_factory in scenarios:
        skill = make_skill()
        defects = defect_factory()
        state_file = make_state_file(f"demo3-{label[0]}.md")
        if state_file.path.exists():
            state_file.path.unlink()
        loop = Loop(
            name=label,
            trigger=Trigger("ci_failure", "CI 红了"),
            workspace=Workspace(branch=f"loop/{label[0]}"),
            skill=skill,
            verifier=Verifier(defects, review_threshold=4.0),
            defects=defects,
            heartbeat=heartbeat,
            state_file=state_file,
            worker=worker_factory(skill),
        )
        report = loop.run("让 src/agent.py 通过所有检查")
        rows.append(
            {
                "场景": label,
                "轮数": report.state.iteration,
                "花费": f"${report.state.spent_usd:.2f}",
                "停下原因": report.brake,
                "结局": report.outcome,
                "没修完的": unfixed(defects, limit=1),
            }
        )

    print()
    trace.section("四个坏掉的 loop")
    trace.summary_table(rows, ["场景", "轮数", "花费", "停下原因", "结局", "没修完的"])

    print()
    trace.note("A 和 B 的差别只有执行者不同。A 的结局是「已转人工」，B 是「未完成」——")
    trace.note("看着差不多，实际差很远：")
    trace.note("")
    trace.note("  A 是执行者**知道自己不行**，带着完整上下文交给人")
    trace.note("  B 是被心跳**从外面掐断**的，人接手时不知道它试过什么")
    trace.note("")
    trace.note("注意 B 其实也没做错什么——它把能修的都修了，只是认不出剩下那个")
    trace.note("不是代码问题。而心跳兜住了它：分数连续 2 轮没动，就停。")
    trace.note("")
    trace.note("但心跳是一道**兜底**，不该是主要的停止机制。")
    trace.note("一个总是被心跳掐断的 loop，说明它的执行者根本没在判断。")
    print()
    trace.note("C 也值得看一眼：它花了 $0.30 就停了，**没有超支**。")
    trace.note("因为预算是每轮开始**前**问的——「再跑一轮会不会超」。")
    trace.note("如果改成跑完再算账，它就会先把钱花掉，然后告诉你超了。")
    trace.note("这个差别在真实系统里很实在：账单总是晚一步，但拦截可以早一步。")


# ==========================================================================
# 演示 4：状态文件 —— 跨会话续跑
# ==========================================================================


def demo_4_state_file() -> None:
    trace.section(
        "演示 4：状态文件——loop 的公共记忆",
        "笔记 13.1.3：这个最容易被忽略，但它才是 loop 能长期跑下去的关键",
    )
    trace.note("笔记的原话：「现在绝大多数 AI 工作流每天都从零开始：")
    trace.note("昨天确认过的事实今天再查一遍，上周否掉的标题风格这周又冒出来，")
    trace.note("某个来源一直不靠谱但它下次还引用。为什么？因为记忆只活在单次对话里，")
    trace.note("对话一关就没了。」")

    def build(worker, heartbeat, defects, state_file, use_memory):
        loop = Loop(
            name="ci-fix-loop",
            trigger=Trigger("ci_failure", "CI 红了"),
            workspace=Workspace(branch="loop/142"),
            skill=make_skill(),
            verifier=Verifier(defects, review_threshold=4.0),
            defects=defects,
            heartbeat=heartbeat,
            state_file=state_file,
            worker=worker,
        )
        return loop.run("让 src/agent.py 通过所有检查", use_memory=use_memory)

    # ---- 第一次运行：钱不够，半路停下 ----
    state_file = make_state_file("demo4-STATE.md")
    if state_file.path.exists():
        state_file.path.unlink()

    print()
    trace.section("第一次运行：预算不够，半路停下")
    defects_first = make_defects()
    first = build(
        Worker("codex", make_skill()),
        Heartbeat(max_iterations=10, no_progress_limit=3, budget_usd=0.30, cost_per_iteration=0.15),
        defects_first,
        state_file,
        use_memory=True,
    )
    show_history(first.state)
    print()
    trace.kv("停下原因", first.brake)
    trace.kv("没修完的", unfixed(defects_first))
    trace.kv("状态文件已写入", f"{state_file.writes} 次")
    trace.note("进程到这里就结束了。但状态文件留下来了——这就是「公共记忆」的意思。")

    # ---- 「第二天」重启：两组各跑 3 次，一组读状态、一组不读 ----
    print()
    trace.section("「第二天」重启：带着记忆 vs 从零开始")

    restarts = 3
    runs: dict[str, tuple[int, int]] = {}
    for label, use_memory in (("不读状态文件", False), ("读状态文件", True)):
        # 两组各用各的状态文件，互不污染。
        # 「不读」那一组照样会写——它只是**从来不回头看**，
        # 而现实里绝大多数 AI 工作流就是这个状态：写了很多，一次都不读。
        group_state = make_state_file(f"demo4-{'read' if use_memory else 'no'}.md")
        if group_state.path.exists():
            group_state.path.unlink()
        total_rounds = 0
        total_tokens = 0
        for _ in range(restarts):
            report = build(
                Worker("codex", make_skill()),
                Heartbeat(max_iterations=10, no_progress_limit=3, budget_usd=5.0),
                make_defects(),
                group_state,
                use_memory=use_memory,
            )
            total_rounds += report.state.iteration
            total_tokens += report.state.tokens
        runs[label] = (total_rounds, total_tokens)

    print()
    trace.summary_table(
        [
            {
                "做法": label,
                f"{restarts} 次重启累计轮数": rounds,
                "累计 token": tokens,
                "每次平均": f"{rounds / restarts:.1f} 轮",
            }
            for label, (rounds, tokens) in runs.items()
        ],
        ["做法", f"{restarts} 次重启累计轮数", "累计 token", "每次平均"],
    )

    print()
    base_rounds, base_tokens = runs["不读状态文件"]
    save_rounds, save_tokens = runs["读状态文件"]
    trace.kv("省下的轮数", f"{base_rounds - save_rounds} 轮（{(base_rounds - save_rounds) / base_rounds:.0%}）")
    trace.kv("省下的 token", f"{base_tokens - save_tokens}（{(base_tokens - save_tokens) / base_tokens:.0%}）")
    trace.note("省的是哪一轮？就是「先试一个通用做法、失败了、才知道要去翻手册」那一轮。")
    trace.note("状态文件把这个结论记下来了，所以下一次不用重新推导。")
    print()
    trace.note("单次看着不多。但它省的是**每一次重启都省一轮**——")
    trace.note("笔记那句话的落点就在这：「一个 skill 写得好的 loop 会开始产生复利」，")
    trace.note("状态文件也一样，而且它的复利是累加的。")

    print()
    trace.section("状态文件的实际内容")
    print(state_file.path.read_text(encoding="utf-8"))
    trace.note(f"文件在：{state_file.path}")
    trace.note("你可以在两次运行之间手动改它——比如把「踩过的坑」删掉，")
    trace.note("再跑一次，就能看到 loop 重新走一遍试错。这比读笔记直观得多。")


# ==========================================================================
# 演示 5：Graph —— 把多个 loop 编排成一张执行图
# ==========================================================================


def build_review_graph(control: ControlGraph) -> Graph:
    """笔记 13.2.2 那张「自动代码审查」图。

        Codex 改代码建 PR
          → 并行启动多个审计 Agent（安全 / 逻辑性能 / 风格文档）
          → 结果汇聚到 Verifier
          → 确认的问题交给 Fixer
          → 测试套件执行外部检查
          → 测试失败回到 Fixer（形成循环）；通过则合并发布

    最后一环「发布前人工检查」是笔记 13.2.4 的要求，也是**控制图接进执行图**的地方：
    它不是图外面的一个流程说明，而是图上一个真的能拦住发布的节点。
    """
    # (问题 id, 属于哪一类, 哪个审计能发现它；None = 三个审计都没覆盖)
    issues: list[tuple[str, str, str | None]] = [
        ("sql-injection", "安全", "安全审计"),
        ("n-plus-one", "性能", "逻辑性能审计"),
        ("naming", "风格", "风格文档审计"),
        ("concurrency-oversell", "并发", None),  # ← 谁都没覆盖到
    ]

    graph = Graph("自动代码审查", entry="writer")

    def writer(state: GraphState) -> FanOut:
        state.say("Codex 改完下单流程，开了 PR #142")
        return FanOut(["audit_security", "audit_logic", "audit_style"], join="verifier")

    def make_audit(name: str):
        def audit(state: GraphState) -> None:
            hits = [i for i in issues if i[2] == name]
            for issue_id, kind, _ in hits:
                state.findings.append({"from": name, "issue": issue_id, "kind": kind})
            state.say(f"{name} 报了 {len(hits)} 条")
            return None

        return audit

    def verifier(state: GraphState) -> str:
        state.confirmed = list(state.findings)
        state.findings = []
        state.say(f"汇聚后确认 {len(state.confirmed)} 条")
        return "fixer" if state.confirmed else "tests"

    def fixer(state: GraphState) -> str:
        state.fix_rounds += 1
        fixed = [f["issue"] for f in state.confirmed]
        state.fixes.extend(fixed)
        state.confirmed = []
        state.say(f"Fixer 第 {state.fix_rounds} 轮修了 {fixed}")
        return "tests"

    def tests(state: GraphState) -> str:
        remaining = [i[0] for i in issues if i[0] not in state.fixes]
        if not remaining:
            state.test_passed = True
            state.say("测试套件全绿")
            return "human_review"
        # 测试失败：它发现了一个三个审计都没覆盖的问题 —— 回到 Fixer，形成循环
        state.test_passed = False
        state.confirmed = [{"from": "测试套件", "issue": remaining[0], "kind": "并发"}]
        state.say(f"测试套件失败，暴露出 {remaining[0]}（三个审计都没覆盖）→ 回环到 Fixer")
        return "fixer"

    def human_review(state: GraphState) -> str | None:
        """现实锚点里最贵、也最不能省的那一个。"""
        if control.vetoes:
            state.fixes.append("veto")
            state.say(f"人工检查叫停：{control.vetoes[-1]}")
            return None  # 停在这里，不发布
        state.say("人工检查通过")
        return "ship"

    def ship(state: GraphState) -> None:
        state.say("合并发布")
        return None

    graph.add_node("writer", writer, "agent", "Codex 写代码 / 开 PR")
    graph.add_node("audit_security", make_audit("安全审计"), "agent", "审计·安全")
    graph.add_node("audit_logic", make_audit("逻辑性能审计"), "agent", "审计·逻辑性能")
    graph.add_node("audit_style", make_audit("风格文档审计"), "agent", "审计·风格文档")
    graph.add_node("verifier", verifier, "verifier", "Verifier 汇聚")
    graph.add_node("fixer", fixer, "agent", "Fixer 修复")
    graph.add_node("tests", tests, "deterministic", "测试套件（外部检查）")
    graph.add_node("human_review", human_review, "human", "发布前人工检查")
    graph.add_node("ship", ship, "deterministic", "合并发布")
    return graph


def demo_5_graph() -> None:
    trace.section(
        "演示 5：Graph——当一个 Loop 不够时",
        "笔记 13.2.2：Graph 不是 Loop 的对立面，因为 Graph 本身可以包含 Loop",
    )

    control = ControlGraph()
    control.add(RealityAnchor("测试套件 + 类型检查", "deterministic", "不依赖模型判断，同样输入必然同样输出"))
    control.add(RealityAnchor("发布前人工检查", "human", "对高风险改动保留否决权"))
    control.add(RealityAnchor("下单转化率 / 超卖告警", "business_metric", "生产环境里的真实结果"))
    graph = build_review_graph(control)

    print()
    trace.section("这张图上有哪些节点")
    trace.summary_table(graph.describe(), ["节点", "类型"])
    trace.note("注意「类型」这一列——它标的是**确定性程度**，不是重要性。")
    trace.note("笔记 13.2.5 的落脚点就是这件事：真正长期有效的能力，")
    trace.note("是判断哪些部分值得交给概率性的 Agent，哪些应该保持朴素、确定且可验证。")

    state = GraphState(pr="#142 重构下单流程")
    result = graph.run(state)

    print()
    trace.section("图是怎么走的")
    print(f"  路径：{' → '.join(state.path)}")
    print()
    for event in state.events:
        trace.bullet(event)

    print()
    trace.kv("修复轮数", state.fix_rounds)
    trace.kv("最终测试", "通过" if state.test_passed else "失败")
    trace.kv("是否发布", "是" if result.shipped else "否")

    print()
    trace.section("控制图：谁来检查做决定的人")
    trace.summary_table(control.audit(state), ["锚点", "类型", "本次结论"])
    print()
    trace.note("笔记 13.2.3：「一套完整的 Graph 不只有『执行图』，还需要『控制图』：")
    trace.note("执行图回答『接下来运行什么』，控制图回答『谁来检查做决定的人』。」")
    trace.note("")
    trace.note("这两张图很容易被混成一张。它们的区别是：")
    trace.note("执行图上的节点在**干活**，控制图上的锚点在**否决**。")

    # ---- 人工否决一次，看它能不能真的拦住发布 ----
    print()
    trace.section("如果人工否决呢")
    control.veto("这次改动动了资金相关的下单链路，需要先用影子流量验证一周")
    veto_state = GraphState(pr="#142 重构下单流程")
    veto_result = graph.run(veto_state)

    print(f"  路径：{' → '.join(veto_state.path)}")
    print()
    for event in veto_state.events:
        trace.bullet(event)
    print()
    trace.kv("最终测试", "通过" if veto_state.test_passed else "失败")
    trace.kv("是否发布", "是" if veto_result.shipped else "否 —— 被人工拦住了")
    trace.note("注意测试是**全绿**的，三个审计 Agent 也都签了字。")
    trace.note("但发布还是没发生——因为控制图上有一个执行图管不着的锚点。")
    trace.note("")
    trace.note("这就是「谁来检查做决定的人」的字面意思：")
    trace.note("执行图上所有节点都说 OK，不等于这件事就该做。")
    trace.note("只有一个执行图的系统，跑得越快、错得越远。")
    trace.note("")
    trace.note("当然，人工检查贵，而且会成为瓶颈。所以笔记 13.2.4 的说法是")
    trace.note("「**在重要发布前**保留最后一次人工检查」——不是每一次，是重要的那次。")

    print()
    trace.note("回到这张图本身，有三件事值得单独指出来：")
    print()
    trace.note("1. **回环是真的回环**。测试失败之后控制流回到了 fixer，")
    trace.note("   而不是往下走到 ship。普通的固定流水线做不到这一点——")
    trace.note("   它的第三步永远在第二步后面。")
    print()
    trace.note("2. **三个审计 Agent 是并行的**。笔记 13.2.3 说，把图显式画出来")
    trace.note("   会迫使我们预先想清楚「哪些任务可以并行运行」——")
    trace.note("   隐性并行（模型自己决定要不要同时干几件事）是没法审计的。")
    print()
    trace.note("3. **抓到问题的不是 Agent，是测试**。三个审计 Agent 各自报了")
    trace.note("   自己那一类问题，然后一致地把并发超卖漏掉了——")
    trace.note("   最后是确定性的测试套件把它翻出来的。")
    trace.note("   这不是巧合，是演示 6 要讲的东西。")


# ==========================================================================
# 演示 6：控制图 —— 「工业化规模的彼此赞同」
# ==========================================================================


def demo_6_consensus() -> None:
    trace.section(
        "演示 6：20 个 Agent 为什么会「以工业化规模彼此赞同」",
        "笔记 13.2.4：不要把「更多 Agent」误认为「更高质量」",
    )
    trace.note("笔记的原话：「20 个使用同一模型、读取同一份错误上下文的 Agent，")
    trace.note("可能会以工业化规模彼此赞同。」")
    trace.note("")
    trace.note("这句话可以直接量化。两组审计 Agent，参数**完全一样**——")
    trace.note("单个人的漏检率都是 35%、误报率都是 30%，人数都是 3 个。")
    trace.note("唯一的差别是：一组的模型和上下文同源，另一组不同源。")

    print()
    trace.kv("同质组", "3 个 Agent，同一个模型、同一份上下文（independence=0）")
    trace.kv("异构组", "3 个 Agent，不同模型、互相独立的新鲜上下文（independence=1）")

    trials = 20000
    rows = []
    for label, homogeneous in (("同质（同模型同上下文）", True), ("异构（不同模型独立上下文）", False)):
        reviewers: list[Reviewer] = make_reviewers(homogeneous=homogeneous)
        for rule in AGGREGATIONS:
            stats = run_consensus_trials(reviewers, rule, trials=trials)
            rows.append(
                {
                    "Reviewer 组": label,
                    "汇聚规则": rule,
                    "漏检率": f"{stats['漏检率']:.1%}",
                    "误报固化率": f"{stats['误报固化率']:.1%}",
                }
            )

    print()
    trace.summary_table(rows, ["Reviewer 组", "汇聚规则", "漏检率", "误报固化率"])
    trace.note("两个方向都是「越低越好」：漏检 = 真问题被放过去，误报固化 = 假问题被当成真的。")
    trace.note(f"每组跑了 {trials} 次，所以是估计值。理论值很好算——")
    trace.note("同质组无论什么规则都等于单个人（35% / 30%），")
    trace.note("异构组「任意一个」的漏检率是 0.35³ ≈ 4.3%，「全体一致」的误报率是 0.30³ = 2.7%。")

    print()
    trace.section("这张表说了三件事")
    print()
    trace.note("**第一，独立性是前提，不是加分项。**")
    trace.note("看同质组那三行——它们一模一样。")
    trace.note("换成「任意一个」「多数」「全体一致」，结果一点没变。")
    trace.note("因为三份拷贝还是**一份判断**：一个人漏了，三个人一起漏；")
    trace.note("一个人误报，三个人一起附和。汇聚规则再精巧也没用，")
    trace.note("它汇聚的本来就是同一个东西。")
    print()
    trace.note("**第二，汇聚规则要选对方向，而两个方向是相反的。**")
    trace.note("看异构组：要召回（别漏掉真问题）就用「任意一个」，漏检率 4.2%；")
    trace.note("要精确（别误报）就用「全体一致」，误报率 2.7%。")
    trace.note("一个要求「有人说有就算有」，另一个要求「所有人都说有才算有」——")
    trace.note("它们不可能同时成立，所以你必须先想清楚这次最怕哪种错。")
    print()
    trace.note("**第三，这也正是笔记说「而非盲从单一多数意见」的原因。**")
    trace.note("「多数」这一行在两个方向上都输给了某个极端：")
    trace.note("漏检 28.3%（比「任意一个」的 4.2% 差了近七倍），")
    trace.note("误报 21.8%（比「全体一致」的 2.7% 差了八倍）。")
    trace.note("它不是一个折中，而是两头不讨好——")
    trace.note("因为「多数」这个规则本身没有方向，它只是在数人头。")

    print()
    trace.section("那正确的做法是什么")
    trace.note("笔记 13.2.4 给的处方，逐条对上前面的数字：")
    print()
    trace.summary_table(
        [
            {"处方": "不同模型的多个 Reviewer", "对应哪一行": "异构 vs 同质", "为什么": "同源 = 同一双眼睛的三份拷贝"},
            {"处方": "彼此独立的新鲜上下文", "对应哪一行": "independence=1", "为什么": "共享上下文 = 共享盲区"},
            {"处方": "合并判断，不盲从多数意见", "对应哪一行": "任意一个 / 全体一致", "为什么": "多数没有方向，两头不讨好"},
            {"处方": "重要发布前保留人工检查", "对应哪一行": "控制图的 human 锚点", "为什么": "总得有个判断不来自模型"},
        ],
        ["处方", "对应哪一行", "为什么"],
    )

    print()
    trace.note("最后一条是最容易被省掉的。它也确实最贵——要占用人的时间。")
    trace.note("但演示 5 里那个并发超卖的例子说明了一件事：")
    trace.note("**三个审计 Agent 全都没覆盖到它**，最后是确定性的测试套件抓出来的。")
    trace.note("")
    trace.note("测试套件是一种现实锚点：它不推理、不判断、不解释，")
    trace.note("只是同样输入给同样输出。正因为这样，它不会跟着 Agent 一起犯错。")
    trace.note("")
    trace.note("不过它也有边界——**它只覆盖它写到的那些情况**。")
    trace.note("并发超卖能被抓到，是因为有人写了那条测试用例。")
    trace.note("没写的那部分，它一样是瞎的。")
    trace.note("所以笔记那句结语不是套话：追求质量时，不可能把所有事情都自动化。")


def main() -> None:
    trace.section(
        "原型 08：Loop 工程 & Graph 工程",
        "笔记第 13 章：五积木 + 记事本、Loop 与 Graph 的边界、执行图与控制图",
    )
    trace.note("这一章笔记有一句自我评价：「其实它并没有什么新东西」，")
    trace.note("以及一句提醒：「你也不能不会」。这个原型两个目的都照顾到了——")
    trace.note("把概念拆成代码之后，你会发现它确实不新，但确实值得写清楚。")

    demo_1_five_bricks()
    demo_2_cron_vs_loop()
    demo_3_heartbeat()
    demo_4_state_file()
    demo_5_graph()
    demo_6_consensus()


if __name__ == "__main__":
    main()
