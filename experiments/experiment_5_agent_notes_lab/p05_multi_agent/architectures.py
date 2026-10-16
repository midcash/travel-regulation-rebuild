"""五种 Agent 架构，用同一套指标量出来对比。对应笔记第 6 章。

笔记引的是 Google 2025 年 12 月的《Towards a Science of Scaling Agent Systems》，
它用四个可量化的协调指标来横评架构：

    LLM 调用次数   花了多少算力
    顺序深度       串行了几步（越长越容易"一步错步步错"）
    通信开销       架构本身带来的协调成本（"协调税"）
    错误放大       子 Agent 的错误在最终结果里被放大/缩小了多少倍

这个文件把这四个指标都实现出来。核心结论（笔记反复强调）：

    **多 Agent 不一定比单 Agent 好。**
    在强顺序、长推理链、单体基线已经不错（论文给的阈值约 45%）、
    工具密集且预算固定的场景下，盲目上多 Agent 反而会掉性能。

所以下面不是"五个架构谁最好"的排名，而是"每个架构在什么条件下划算"。

模拟方式：给每个子 Agent 一个固定的错误率 p，用固定种子的随机数跑 N 次试验，
统计最终结果错了几次。这样错误放大倍数是可以算出来、也能在调试器里复现的。
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class AgentSpec:
    """一个子 Agent。"""

    name: str
    error_rate: float = 0.25  # 单次任务出错的概率
    role: str = ""


@dataclass
class Ledger:
    """架构运行时的记账本：每做一件事就记一笔，指标是**数出来的**。

    四个协调指标里，`correct`（错误放大）一直是真测的；另外三个以前是
    **手写常量** —— `llm_calls=3`、`tokens=900`、`sequential_depth=2`。

    常量的问题不是"不准"，是**不会随代码变**。你把流程从 3 步改成 5 步，
    那个 3 还老老实实待在那儿，而表格照样印出来，看起来和真测的一样。

    这里的分工是：

        单位成本（一次调用多少 token）—— 可以假设，它是参数；
        总量（一共几次调用、几层、几条消息）—— 必须由跑的过程数出来。

    假设写在参数里，谁都能看见；结论写在表格里，必须是被算出来的。
    """

    tokens_per_call: int = 300
    llm_calls: int = 0
    messages: int = 0
    tokens: int = 0
    depth: int = 0

    def reason(self, count: int = 1) -> "Ledger":
        """记**一层** LLM 调用：这一层里有 count 次调用。

        count=1 就是一步一步串行；count=3 表示这三个子 Agent 是**并发**跑的 ——
        调用次数照加，顺序深度只算一层。

        这正是"多 Agent 并行"在指标上的样子：算力翻倍，串行长度不变。
        也是为什么顺序深度不能等于调用次数。
        """
        self.llm_calls += count
        self.tokens += count * self.tokens_per_call
        self.depth += 1
        return self

    def talk(self, count: int = 1) -> "Ledger":
        """记 count 条 Agent 间消息 —— 也就是笔记说的「协调税」。"""
        self.messages += count
        return self

    def as_metrics(self) -> dict[str, int]:
        """交给 Outcome 的四个字段。runner 直接 `**ledger.as_metrics()` 就行。"""
        return {
            "llm_calls": self.llm_calls,
            "sequential_depth": self.depth,
            "messages": self.messages,
            "tokens": self.tokens,
        }


def _trial(agent: AgentSpec, rng: random.Random) -> bool:
    """跑一次子 Agent，返回是否做对。

    注意它和 Ledger.reason() 是**两件事**：
      - reason() 数的是"结构上发了几次 LLM 调用"，由代码路径决定；
      - _trial() 抽的是"这一次做对了没有"，是错误率的抽样。

    每次 LLM 调用都抽一次签，会让错误率随架构步数一起变，
    整张表就没法和笔记里引的论文数字对照了。所以抽签次数被刻意
    钉在"每个子 Agent 一次"上 —— 这条口径在改动前后必须保持不变，
    变的只是那三个**被数出来**的指标。
    """
    return rng.random() >= agent.error_rate


@dataclass
class Outcome:
    """一次架构运行的结果。四个指标都在这里。"""

    name: str
    correct: bool
    llm_calls: int
    sequential_depth: int
    messages: int
    tokens: int
    detail: str = ""
    subtask_results: list[bool] = field(default_factory=list)

    @property
    def communication_overhead(self) -> int:
        """通信开销：架构本身带来的协调消息数，不含任务本身的内容。"""
        return self.messages


# --------------------------------------------------------------------------
# 1) SAS：单体架构
# --------------------------------------------------------------------------


def run_sas(agents: list[AgentSpec], rng: random.Random) -> Outcome:
    """一个 Agent 顺序地感知 -> 推理/规划 -> 行动 -> 迭代。

    LLM 调用 O(T)，顺序深度 T，通信开销 0，没有并行。
    最稳、最便宜。工具密集和强顺序的任务用它。

    签名和其他 runner 保持一致（都收 list），这样 simulate() 可以统一调用，
    只取第一个 Agent —— 单体架构本来就只有一个人。
    """
    agent = agents[0]
    ledger = Ledger(tokens_per_call=300)
    ledger.reason()  # 规划
    ok = _trial(agent, rng)
    ledger.reason()  # 执行
    ledger.reason()  # 迭代 / 自检
    ledger.talk(0)  # 没有 Agent 间通信 —— 记一笔 0，让"零开销"也是数出来的
    return Outcome(
        name="SAS 单体",
        correct=ok,
        **ledger.as_metrics(),
        detail="一个 Agent 从头做到尾",
        subtask_results=[ok],
    )


# --------------------------------------------------------------------------
# 2) MAS(Independent)：独立多体
# --------------------------------------------------------------------------


def run_independent_synthesis(agents: list[AgentSpec], rng: random.Random) -> Outcome:
    """多个 Agent 各自独立做**不同**的子任务，最后拼起来。

    这是错误放大最严重的形态：每个子任务的错误都会原样进入最终结果，
    拼装时被"叠加"。笔记引论文的数字是 17.2x。
    """
    ledger = Ledger(tokens_per_call=350)
    results = [_trial(agent, rng) for agent in agents]
    ledger.reason(len(agents))  # 一层：各自做自己的子任务（并行）
    ledger.reason(len(agents))  # 一层：各自整理并交付（并行）
    ledger.talk(len(agents))  # 每个 Agent 交一次结果
    return Outcome(
        name="独立多体（分工拼装）",
        correct=all(results),
        **ledger.as_metrics(),  # 深度不随人数增长：两次 reason(3) 只算两层
        detail=f"{len(agents)} 个子任务拼装，任意一个错则整体错",
        subtask_results=results,
    )


def run_independent_vote(agents: list[AgentSpec], rng: random.Random) -> Outcome:
    """多个 Agent 独立做**同一个**任务，再投票。

    和上面那种"分工拼装"完全不同：这里用集体投票对抗个体幻觉，
    物理隔离保证了推理独立性。错误率是被**压下去**的。

    两者常被混为一谈，是面试里的一个坑。
    """
    ledger = Ledger(tokens_per_call=350)
    results = [_trial(agent, rng) for agent in agents]
    yes = sum(results)
    ledger.reason(len(agents))  # 一层：同时做同一件事（并行）
    ledger.reason(len(agents))  # 一层：各自给出结论（并行）
    ledger.talk(len(agents))  # 每个 Agent 报一次票
    return Outcome(
        name="独立多体（同任务投票）",
        correct=yes * 2 > len(results),  # 多数票
        **ledger.as_metrics(),
        detail=f"{len(agents)} 个 Agent 做同一件事，{yes} 票通过（多数决）",
        subtask_results=results,
    )


# --------------------------------------------------------------------------
# 3) MAS(Centralized)：中心化
# --------------------------------------------------------------------------


def run_centralized(
    agents: list[AgentSpec],
    rng: random.Random,
    gate_catch_rate: float = 0.8,
) -> Outcome:
    """有一个主管：分解任务 -> 分派 -> 收集 -> 验收 -> 裁决。

    关键在于主管有一道**质量门控**：子 Agent 的错误有 gate_catch_rate 的概率
    在验收环节被抓出来并返工。这就是中心化能把错误放大压下去的原因
    （论文里从 17.2x 压到 4.4x）。

    代价是多出一层"返工"，而且主管本身可能成为瓶颈。

    （这里原本还有个 `rounds` 参数，唯一的作用是凑出 sequential_depth 那个常量。
      深度现在是数出来的，它就没有存在的理由了 —— 一个只用来生成假数字的参数，
      留着比删掉更危险。）
    """
    ledger = Ledger(tokens_per_call=800)
    ledger.reason(len(agents))  # 一层：n 个子 Agent 并发干活

    results: list[bool] = []
    rejected = 0
    for agent in agents:
        ok = _trial(agent, rng)
        if not ok and rng.random() < gate_catch_rate:
            rejected += 1
            # 被主管打回，返工一次
            ok = _trial(agent, rng)
        results.append(ok)

    if rejected:
        ledger.reason(rejected)  # 又一层：被打回的那些并行返工
    ledger.reason()  # 最后：主管的分解 + 汇总
    ledger.talk(len(agents) * 2 + 1)  # 派活 + 交活 + 一次汇总
    return Outcome(
        name="中心化（主管编排）",
        correct=all(results),
        **ledger.as_metrics(),
        detail=f"主管验收打回 {rejected} 个不合格结果并要求返工",
        subtask_results=results,
    )


# --------------------------------------------------------------------------
# 4) MAS(Decentralized)：去中心化
# --------------------------------------------------------------------------


def run_decentralized(
    agents: list[AgentSpec],
    rng: random.Random,
    debate_rounds: int = 2,
    peer_fix_rate: float = 0.3,
) -> Outcome:
    """Agent 之间点对点辩论/互评，多轮质询后收敛。

    每个错误每轮有 peer_fix_rate 的概率被同伴指出并改对。
    这个概率是"发现问题"和"能改对"两件事的乘积，所以不该设得太高 ——
    同伴未必看得出你错在哪，看出来了也未必改得对。

    适合信息动态、需要探索与互评的任务；代价是通信税很重。
    """
    ledger = Ledger(tokens_per_call=600)
    results = [_trial(agent, rng) for agent in agents]
    ledger.reason(len(agents))  # 一层：n 个 Agent 并发给出各自的判断

    messages = 0
    for _ in range(debate_rounds):
        ledger.reason(len(agents))  # 每一轮辩论都是新的一层（所有人在这一层里并行表态）
        for index in range(len(results)):
            messages += len(agents) - 1  # 每轮每人跟其他所有人交换一次意见
            if not results[index] and rng.random() < peer_fix_rate:
                results[index] = True

    yes = sum(results)
    ledger.talk(messages + len(agents))
    return Outcome(
        name="去中心化（点对点辩论）",
        correct=yes * 2 > len(results),
        **ledger.as_metrics(),
        detail=f"{debate_rounds} 轮互评后 {yes} 票通过",
        subtask_results=results,
    )


# --------------------------------------------------------------------------
# 5) MAS(Hybrid)：层级 + 有限对等
# --------------------------------------------------------------------------


def run_hybrid(
    agents: list[AgentSpec],
    rng: random.Random,
    gate_catch_rate: float = 0.8,
    peer_fix_rate: float = 0.4,
) -> Outcome:
    """层级控制 + 有限对等通信。在中心化的门控之上，再加一层同伴互评。

    通常还要配通信限流/白名单，避免消息爆炸（messages 那一列会立刻涨上去）。
    """
    ledger = Ledger(tokens_per_call=800)
    ledger.reason(len(agents))  # 一层：各自先做
    results = [_trial(agent, rng) for agent in agents]

    messages = 0
    peer_fixed = 0
    gate_reworked = 0
    for index, agent in enumerate(agents):
        if not results[index] and rng.random() < peer_fix_rate:
            results[index] = True
            messages += 1  # 同伴指出问题
            peer_fixed += 1
        if not results[index] and rng.random() < gate_catch_rate:
            results[index] = _trial(agent, rng)
            messages += 2  # 主管打回 + 重新提交
            gate_reworked += 1

    # 返工分两层：先"同伴指出的那些"并行重做，再"主管打回的"并行重做。
    # 这两层在时间里是分开的，所以顺序深度要分别记 ——
    # 以前那个常量 3 是把它们拍扁成一步了。
    if peer_fixed:
        ledger.reason(peer_fixed)
    if gate_reworked:
        ledger.reason(gate_reworked)
    ledger.reason()  # 最后：主管汇总

    ledger.talk(messages + len(agents) + 1)
    return Outcome(
        name="混合（层级 + 有限对等）",
        correct=all(results),
        **ledger.as_metrics(),
        detail="先同伴互评，再由主管门控",
        subtask_results=results,
    )


# --------------------------------------------------------------------------
# 模拟：跑很多次，统计指标
# --------------------------------------------------------------------------


@dataclass
class ArchitectureStats:
    name: str
    error_rate: float
    amplification: float
    avg_llm_calls: float
    avg_messages: float
    avg_tokens: float
    sequential_depth: int
    detail: str = ""

    def as_row(self) -> dict[str, str]:
        return {
            "架构": self.name,
            "最终错误率": f"{self.error_rate:.1%}",
            "错误放大": f"{self.amplification:.2f}x",
            "平均LLM调用": f"{self.avg_llm_calls:.1f}",
            "通信消息数": f"{self.avg_messages:.1f}",
            "平均token": f"{self.avg_tokens:.0f}",
            "顺序深度": str(self.sequential_depth),
        }


def simulate(
    runner: Callable[[list[AgentSpec], random.Random], Outcome],
    agents: list[AgentSpec],
    trials: int = 4000,
    seed: int = 20260911,
) -> ArchitectureStats:
    """把同一个架构跑 trials 次，统计四个指标。"""
    rng = random.Random(seed)
    failures = 0
    total_calls = total_messages = total_tokens = 0
    depth = 0
    detail = ""
    name = runner.__name__

    for _ in range(trials):
        outcome = runner(agents, rng)
        if not outcome.correct:
            failures += 1
        total_calls += outcome.llm_calls
        total_messages += outcome.messages
        total_tokens += outcome.tokens
        # 顺序深度取**最坏情况**（所有试验里的最大值），不是最后一轮的值。
        # 它衡量的是"最长串行路径"，是个结构属性：
        # 某一轮恰好没人返工，不代表这个架构的串行长度变短了。
        # （以前这里是 `depth = outcome.sequential_depth`，等于让最后一次试验
        #   单独决定整张表的这一列 —— 那种"指标"是抽签抽出来的。）
        depth = max(depth, outcome.sequential_depth)
        detail = outcome.detail
        name = outcome.name  # 架构名以 Outcome 为准，别用函数名

    error_rate = failures / trials
    per_agent = agents[0].error_rate
    return ArchitectureStats(
        name=name,
        error_rate=error_rate,
        # 错误放大 = 最终错误率 / 单个子 Agent 的错误率。
        # 大于 1 表示架构在放大错误，小于 1 表示架构在吸收错误。
        amplification=error_rate / per_agent if per_agent else 0.0,
        avg_llm_calls=total_calls / trials,
        avg_messages=total_messages / trials,
        avg_tokens=total_tokens / trials,
        sequential_depth=depth,
        detail=detail,
    )
