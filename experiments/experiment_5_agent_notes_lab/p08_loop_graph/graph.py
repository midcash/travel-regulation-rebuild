"""Graph 工程：把多个 loop 编排成一张图。对应笔记 13.2。

笔记 13.2.2 把 Loop 和 Graph 的分工说得很清楚：

    Loop 是完成一个任务的反馈闭环；
    Graph 是组织多个任务、角色、分支和反馈闭环的更高一层结构。

    当一个 Loop 无法完成复杂任务时，Graph 的讨论就开始了。
    Graph 不是 Loop 的对立面，因为 **Graph 本身可以包含 Loop**。

笔记给的那张自动代码审查的图，就是这个文件实现的东西：

    Codex 完成代码修改并创建 PR
      → 并行启动多个审计 Agent（安全 / 逻辑性能 / 风格文档）
      → 结果汇聚到 Verifier
      → 确认的问题交给 Fixer 修复
      → 测试套件执行外部检查
      → 测试失败回到 Fixer（形成循环）；通过则合并发布

还有一句必须落进代码的话（13.2.3）：

    一套完整的 Graph 不只有「执行图」，还需要「控制图」：
    执行图回答「接下来运行什么」，控制图回答「谁来检查做决定的人」。

以及 13.2.4 的两条最佳实践，这个文件用两个演示分别验证：

    不要把「更多 Agent」误认为「更高质量」。20 个使用同一模型、
    读取同一份错误上下文的 Agent，可能会以工业化规模彼此赞同。

    至少保留一个 Graph 之外的现实锚点——一种不属于同一套概率性自证循环的检查。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Callable


def _unit(*parts: object) -> float:
    """把一组 key 确定性地映射到 [0,1)。理由同 p07：连续抽样会让决策点互相耦合。"""
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


# ==========================================================================
# 执行图
# ==========================================================================


@dataclass
class FanOut:
    """并行：同时跑这几个节点，结果汇聚到 join。

    笔记 13.2.3 说，把图显式画出来会迫使我们预先想清楚：
    「哪些任务可以并行运行？节点之间传递什么状态？」

    这个类就是第一个问题的答案，它必须被**写下来**——
    隐性并行（模型自己决定要不要同时干几件事）是没法审计的。
    """

    nodes: list[str]
    join: str


@dataclass
class Node:
    """图上的一个节点。

    kind 决定了这个节点的**确定性程度**，这是 Graph 工程里最容易被忽略的维度：

        agent          概率性的。Agent 会解释任务，可能误解指令，
                       也可能下一次运行做出不同选择（笔记 13.2.3 原话）
        verifier       汇聚判断，仍然是概率性的
        deterministic  确定性的。测试、类型检查、规则校验——同样输入必然同样输出
        human          人的判断，拥有否决权

    笔记 13.2.5 结语的落脚点就是这件事：
    「真正长期有效的能力，是判断系统中的哪些部分值得交给概率性的 Agent，
    哪些部分应该继续保持朴素、确定且可验证。」
    """

    name: str
    fn: Callable[["GraphState"], Any]
    kind: str = "agent"
    label: str = ""


@dataclass
class GraphState:
    """在图里流转的状态。笔记 13.2.3：「节点之间传递什么状态？」"""

    pr: str
    findings: list[dict[str, Any]] = field(default_factory=list)
    confirmed: list[dict[str, Any]] = field(default_factory=list)
    fixes: list[str] = field(default_factory=list)
    test_passed: bool = False
    fix_rounds: int = 0
    path: list[str] = field(default_factory=list)
    events: list[str] = field(default_factory=list)

    def say(self, text: str) -> None:
        self.events.append(text)


@dataclass
class GraphResult:
    state: GraphState
    shipped: bool
    stopped_by: str  # ship / human_veto / max_fix_rounds


class Graph:
    """一张执行图：节点承担任务，边决定下一步运行什么。

    笔记 13.2.3：「它与普通固定流水线的关键区别在于：普通流水线中的步骤遵循固定规则，
    而 Graph 中的 Agent 会解释任务。Agent 可能误解指令，也可能在下一次运行时
    做出不同选择，因此**节点具有概率性**。」

    所以 run() 里有一处和流水线本质不同的设计：节点的返回值决定下一步，
    而不是预先写死的顺序。旁路、回环、提前终止都是这么长出来的。
    """

    def __init__(self, name: str, entry: str):
        self.name = name
        self.entry = entry
        self.nodes: dict[str, Node] = {}
        self.edges: list[tuple[str, str, str]] = []
        self.max_steps = 40

    def add_node(
        self,
        name: str,
        fn: Callable[[GraphState], Any],
        kind: str = "agent",
        label: str = "",
    ) -> Node:
        node = Node(name, fn, kind, label or name)
        self.nodes[name] = node
        return node

    def connect(self, src: str, dst: str, label: str = "") -> None:
        self.edges.append((src, dst, label))

    def describe(self) -> list[dict[str, str]]:
        kind_label = {
            "agent": "概率性 Agent",
            "verifier": "汇聚判断",
            "deterministic": "确定性检查",
            "human": "人工",
        }
        return [
            {"节点": node.label, "类型": kind_label.get(node.kind, node.kind)}
            for node in self.nodes.values()
        ]

    def run(self, state: GraphState) -> GraphResult:
        current: Any = self.entry
        steps = 0
        while current and steps < self.max_steps:
            steps += 1
            names = current.nodes if isinstance(current, FanOut) else [current]
            join = current.join if isinstance(current, FanOut) else ""

            nexts: list[Any] = []
            for name in names:
                node = self.nodes[name]
                state.path.append(name)
                if isinstance(current, FanOut):
                    state.say(f"（并行）{node.label}")
                nexts.append(node.fn(state))

            if isinstance(current, FanOut):
                state.say(f"（汇聚）{self.nodes[join].label}")
                current = join
                continue
            current = nexts[0]

        # 「跑完了」不等于「发布了」。控制图上的否决不改变执行图的走向，
        # 它改变的是**结果能不能落地**——这两件事必须在代码里分开，
        # 否则一个人工否决会在报表上显示成一次成功发布。
        vetoed = "veto" in state.fixes
        shipped = state.test_passed and state.confirmed == [] and not vetoed
        if vetoed:
            stopped_by = "human_veto"
        elif state.test_passed:
            stopped_by = "ship"
        else:
            stopped_by = "loopback"
        return GraphResult(state=state, shipped=shipped, stopped_by=stopped_by)


# ==========================================================================
# 控制图与现实锚点
# ==========================================================================


@dataclass
class RealityAnchor:
    """现实锚点：**不属于同一套概率性自证循环**的检查。

    笔记 13.2.4 的原话：

        「扩展 Agent 系统时，至少应有一种检查不属于同一套概率性自证循环，例如：
         确定性的代码测试或代码审查规则；人工检查与否决权；
         可直接观测的业务指标，例如实际收入、成本或其他外部结果。
         这些外部指标和检查用来确认系统没有只是在规模化地产出低质量内容。」

    为什么要单独拎出来？因为一个全由同源 Agent 组成的图，
    内部是可以自洽的——它能一致地同意一件错事。
    锚点的作用就是：**总有一个判断不是从模型里长出来的。**
    """

    name: str
    kind: str  # deterministic / human / business_metric
    detail: str

    def describe(self) -> str:
        labels = {
            "deterministic": "确定性检查",
            "human": "人工否决权",
            "business_metric": "可观测业务指标",
        }
        return f"{self.name}（{labels.get(self.kind, self.kind)}）：{self.detail}"


class ControlGraph:
    """控制图：谁来检查做决定的人。

    笔记 13.2.3：「指标、审计、权限、人类否决权以及来自真实世界的反馈，
    共同为系统提供现实锚点。」

    它和执行图是**两张图**，这很容易被混淆：
    执行图问「接下来运行什么」，控制图问「谁有权否决前面那个决定」。
    只有执行图的系统，跑得越快错得越远。
    """

    def __init__(self):
        self.anchors: list[RealityAnchor] = []
        self.vetoes: list[str] = []

    def add(self, anchor: RealityAnchor) -> RealityAnchor:
        self.anchors.append(anchor)
        return anchor

    def veto(self, reason: str) -> str:
        """人工否决。注意它不需要理由充分——否决权本身就是它的理由。"""
        self.vetoes.append(reason)
        return f"人工否决：{reason}"

    def audit(self, state: GraphState) -> list[dict[str, str]]:
        return [
            {
                "锚点": anchor.name,
                "类型": anchor.describe().split("（")[1].split("）")[0],
                "本次结论": self._verdict(anchor, state),
            }
            for anchor in self.anchors
        ]

    @staticmethod
    def _verdict(anchor: RealityAnchor, state: GraphState) -> str:
        if anchor.kind == "deterministic":
            return "测试通过" if state.test_passed else "测试失败"
        if anchor.kind == "human":
            return "已否决" if state.fixes and "veto" in state.fixes else "未介入"
        return "指标正常"


# ==========================================================================
# 审计 Agent：为什么「更多 Agent」不等于「更高质量」
# ==========================================================================


@dataclass
class Reviewer:
    """一个审计 Agent。

    三个字段决定了它作为「第二双眼睛」到底值不值钱：

        model       用哪个模型审。**同一个模型审自己写的东西，看不出毛病**，
                    因为它的盲区和写的时候是同一片。
        independence 上下文是否独立。读同一份上下文，就会犯同一个错。
        focus       审什么。

    笔记 13.2.4 给的处方是：
    「使用来自不同模型的多个 Reviewer；给 Reviewer 提供彼此独立的新鲜上下文；
      合并多个 Judge 的判断，而非盲从单一多数意见。」
    """

    name: str
    focus: str
    model: str
    miss_rate: float   # 漏掉一个真问题的概率
    false_rate: float  # 把没问题说成有问题的概率
    independence: float  # 0 = 和同伴完全相关（同模型同上下文），1 = 完全独立


#: 汇聚规则。笔记 13.2.4 的原话是「合并多个 Judge 的判断，而非盲从单一多数意见」——
#: 把三个规则并排放，就能看出那句话到底在说什么。
AGGREGATIONS: dict[str, Any] = {
    "任意一个": lambda votes, n: votes >= 1,
    "多数": lambda votes, n: votes * 2 > n,
    "全体一致": lambda votes, n: votes == n,
}


def tally(reviewers: list[Reviewer], trial: int, *, has_real_bug: bool, has_false_alarm: bool) -> dict[str, int]:
    """一组审计 Agent 的原始投票，**不做汇聚**。

    汇聚规则是后面单独一层。这个拆分很重要：
    「三个 Agent 各自说了什么」和「怎么把三句话合成一句」，是两个独立的决定，
    而它们的组合方式决定了这一组 Reviewer 到底值不值钱。

    independence 的实现方式就是随机源：
        independence=0  整组共用 `("shared", trial)` 这一个 key
                        → 三个人的判断一模一样，是同一双眼睛的三份拷贝
        independence=1  各自用 `(reviewer.name, trial)`
                        → 三个独立判断
    """
    found_votes = 0
    false_votes = 0
    for reviewer in reviewers:
        if reviewer.independence >= 1.0:
            key = (reviewer.name, trial, "independent")
        elif reviewer.independence <= 0.0:
            key = ("shared", trial, "correlated")
        else:
            key = (reviewer.name, trial, "partial")
        draw = _unit(*key)

        if has_real_bug and draw >= reviewer.miss_rate:
            found_votes += 1
        if has_false_alarm and draw < reviewer.false_rate:
            false_votes += 1
    return {"found": found_votes, "false": false_votes, "n": len(reviewers)}


def run_consensus_trials(
    reviewers: list[Reviewer],
    rule: str,
    trials: int = 400,
) -> dict[str, float]:
    """跑很多次，量出这一组 Reviewer 在两个方向上的表现。

        漏检率      真问题被放过去了（召回问题）
        误报固化率  不存在的问题被"确认"，然后浪费一轮去修它（精确问题）

    这两个方向**要的汇聚规则是相反的**——这就是那个演示的结论。
    """
    aggregate = AGGREGATIONS[rule]
    missed = 0
    false_confirmed = 0
    for trial in range(trials):
        votes = tally(reviewers, trial, has_real_bug=True, has_false_alarm=True)
        if not aggregate(votes["found"], votes["n"]):
            missed += 1
        if aggregate(votes["false"], votes["n"]):
            false_confirmed += 1
    return {
        "漏检率": missed / trials,
        "误报固化率": false_confirmed / trials,
    }


def make_reviewers(*, homogeneous: bool) -> list[Reviewer]:
    """两组审计 Agent。参数刻意取得一样——差别**只**在模型和上下文是否同源。

    这是这个演示最重要的设计：如果连漏检率都不一样，
    那就说不清结论是「同质」造成的还是「能力差」造成的。
    """
    focuses = [("安全审计", "安全"), ("逻辑性能审计", "逻辑与性能"), ("风格文档审计", "风格与文档")]
    if homogeneous:
        # 同一个模型、同一份上下文。三个名字，一双眼睛。
        return [
            Reviewer(name, focus, model="同一个模型", miss_rate=0.35, false_rate=0.30, independence=0.0)
            for name, focus in focuses
        ]
    # 不同模型、独立的新鲜上下文。三双真正独立的眼睛。
    return [
        Reviewer(name, focus, model=model, miss_rate=0.35, false_rate=0.30, independence=1.0)
        for (name, focus), model in zip(focuses, ["模型A", "模型B", "模型C"])
    ]
