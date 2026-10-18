"""幻觉的七类缓解手段。对应笔记 10.3。

笔记 10.3 开头有一句话定了调：

    既然每一层都无法被彻底「修好」，工程上的策略就是**绕过去、监测它、约束它、复核它**。

七类手段按笔记的顺序：

    1  外部工具 / WebSearch / API / 数据库    让模型不要只靠参数回答
    2  RAG                                   把回答锚定到可信知识库
    3  RLHF / DPO                            重写它的后训练偏好
    4  CoT / Self-Ask                        多轮验证 + 反思
    5  融合模型 / 投票机制                    用群体智慧稀释个体的讨好倾向
    6  事实核查与用户侧验证                    在使用环节建立事实校验机制
    7  H-Neurons 幻觉探测器                   监测而不是直接删除

这个文件把它们按**作用位置**分成了四层，这个分法比按顺序列更有用：

    训练层  RLHF/DPO          —— 改的是模型本身，部署时改不动
    检索层  外部工具 / RAG     —— 改的是模型看到的上下文
    生成层  CoT / 投票         —— 改的是生成过程
    使用层  事实核查 / 监测     —— 改的是「你能不能发现它错了」

最后一句是关键：**第 6、7 类不改变答案，只让风险可见。**
把它们和前面几类混为一谈，就会高估它们的实际效果。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

from model import Answer, Fact, HallucinatingModel, ModelConfig, _unit


# ==========================================================================
# 开关
# ==========================================================================


@dataclass
class Mitigations:
    """七个开关。默认全关——先看裸模型会怎么错，再一个个打开。"""

    external_tools: bool = False   # 1
    rag: bool = False              # 2
    rlhf: bool = False             # 3
    cot: bool = False              # 4
    ensemble: bool = False         # 5
    fact_check: bool = False       # 6
    h_neuron_monitor: bool = False # 7

    LABELS = {
        "external_tools": "1 外部工具",
        "rag": "2 RAG",
        "rlhf": "3 RLHF/DPO",
        "cot": "4 CoT/Self-Ask",
        "ensemble": "5 多模型投票",
        "fact_check": "6 事实核查",
        "h_neuron_monitor": "7 H-Neurons 监测",
    }

    def enabled(self) -> list[str]:
        return [self.LABELS[name] for name in self.LABELS if getattr(self, name)]

    def describe(self) -> str:
        names = self.enabled()
        return " ＋ ".join(names) if names else "（无）"


# ==========================================================================
# 数据源
# ==========================================================================


#: 知识库条目：(内容, 是不是脏数据)
KBEntry = tuple[str, bool]


def make_knowledge_base() -> dict[str, KBEntry]:
    """RAG 检索的知识库。

    注意它的两个性质，后面都会变成结论：
      - 它是**静态的**，所以也有自己的截止日（新规定根本没进去）
      - 它可能**本身就有脏数据**（当初录入的时候就是错的）
    """
    return {
        "公司的退票手续费怎么算？": ("开车前 24 小时以上免手续费", False),
        "员工出差的住宿标准是多少？": ("一线城市每晚 600 元", False),
        "2026 年 3 月新出的报销规定是什么？": ("电子发票需在 30 天内提交", False),
        # 录入的时候就错了。RAG 救不了脏数据，只会把脏数据喂得更理直气壮 ——
        # 因为它现在带着「检索到的资料」这个权威身份出现在上下文里。
        "增值税专票的抵扣期限是多久？": ("自开具之日起 180 天内认证抵扣", True),
        "出差补贴的具体金额是多少？": ("一线城市每天 200 元", False),
        "这笔订单能退多少钱？": ("以订单页显示的退款金额为准", False),
        # 没有「公司 2026 年的差旅预算」——这笔记还没结，知识库里当然没有
    }


def make_live_sources() -> dict[str, KBEntry]:
    """外部工具能查到的实时数据。

    和 RAG 的关键区别：**它是实时的**，所以能覆盖训练截止日之后的事实。
    但它只覆盖「有接口可查」的那部分——没接口的照样查不到。
    """
    return {
        "公司 2026 年的差旅预算是多少？": ("全年 2400 万元", False),   # 财务系统有接口
        "这笔订单能退多少钱？": ("以订单页显示的退款金额为准", False),   # 订单系统有接口
        # 没有「上周发布的新版差旅系统叫什么」—— 这个得问人，系统里查不到
    }


# ==========================================================================
# 管道
# ==========================================================================


@dataclass
class PipelineResult:
    answer: Answer

    @property
    def caught(self) -> bool:
        return self.answer.flagged


class Pipeline:
    """把缓解手段串成一条管道。

    顺序是有讲究的，它对应信息流过系统的方向：

        训练层（改模型） → 检索层（改上下文） → 生成层（改过程） → 使用层（标记风险）

    每一层只能治住特定的成因——这就是这个原型最想说明的一件事。
    """

    def __init__(
        self,
        model: HallucinatingModel,
        mitigations: Mitigations | None = None,
        *,
        knowledge_base: dict[str, KBEntry] | None = None,
        live_sources: dict[str, KBEntry] | None = None,
    ):
        self.m = mitigations or Mitigations()
        self.kb = knowledge_base if knowledge_base is not None else make_knowledge_base()
        self.live = live_sources if live_sources is not None else make_live_sources()

        # ---- 训练层与生成层：这两个改的是**模型配置本身** ----
        config = model.config
        if self.m.rlhf:
            # 笔记 10.3 第 3 条：「显式奖励『承认低置信度』的行为，
            # 让模型在没把握时使用预设回答，而不是编造事实。」
            # 落地就是 answer_bias 从 0.9 降到 0.25 —— 注意它**降的是幻觉，
            # 不是提升准确率**：模型该不知道的还是不知道，只是不编了。
            config = replace(config, answer_bias=0.25)
        if self.m.cot:
            # 笔记 10.3 第 4 条：CoT / Self-Ask 让模型「分配更多计算资源、
            # 进行多轮验证和反思」。落地就是 reasoning 倍数拉高。
            # 注意它只改善**路径模糊**的那些成因，对「没有路径」的无效。
            config = replace(config, reasoning=config.reasoning * 2.5)
        self.model = HallucinatingModel(config) if config is not model.config else model

    # -- 检索层 -------------------------------------------------------------

    def _retrieve(self, fact: Fact) -> tuple[str, bool, str]:
        """返回 (检索到的内容, 是不是脏数据, 谁提供的)。空内容表示没查到。"""
        if self.m.external_tools and fact.q in self.live:
            text, dirty = self.live[fact.q]
            return text, dirty, "外部工具"
        if self.m.rag and fact.q in self.kb:
            text, dirty = self.kb[fact.q]
            return text, dirty, "RAG"
        return "", False, ""

    # -- 生成层 -------------------------------------------------------------

    def _generate(self, fact: Fact, variant: int, sample: int, user_premise: str) -> Answer:
        retrieved, dirty, source = self._retrieve(fact)
        if retrieved and dirty:
            # 检索层不但没帮上忙，还把事情变糟了：
            # 它把一句错话以「资料」的身份放进了上下文。
            # 无检索时模型至少还只是「记得有点模糊」，现在是「有据可依」。
            return Answer(
                retrieved,
                "记忆-污染",
                False,
                self.model.confidence(),
                sample,
                flag_reason=f"检索源 {source} 本身是脏数据，但模型无从判断",
            )
        return self.model.answer(
            fact, variant=variant, sample=sample, user_premise=user_premise, retrieved=retrieved
        )

    def _ensemble(self, fact: Fact, variant: int, user_premise: str) -> Answer:
        """多模型投票（自洽性）。

        做法很朴素：同一个问题采 3 次，看三个答案一不一致。

        它对**随机编造**有效——编出来的东西每次都不一样，票会散掉，
        模型于是改口说「我不确定」。注意这不是答对了，是**不编了**。

        它对**系统性错误**完全无效——所有模型读的是同一份污染语料，
        于是它们**一致地错**，投票还会让这个错误显得更可信：
        三个模型都这么说，你凭什么不信？
        """
        votes = [self._generate(fact, variant, sample, user_premise) for sample in range(3)]
        texts = {v.text for v in votes}
        if len(texts) == 1:
            return votes[0]
        # 三次都没说到一块去 —— 说明它在采样而不是在回忆
        return Answer(
            "（三次采样结果不一致）这个我不确定，建议查一下正式的来源。",
            "不确定性声明",
            False,
            votes[0].confidence * 0.6,
        )

    # -- 使用层 -------------------------------------------------------------

    def _fact_check(self, fact: Fact, variant: int, answer: Answer) -> Answer:
        """事实核查 / 用户侧验证（第 6 类）。

        模型化的做法就是笔记 10.3 里那句「**换一种问法追问**」：
        同一个问题换个问法再问一遍，看两次答案一不一致。

        编造的东西每次都不一样，所以换一次问法就能露馅。
        但**污染的知识很稳定**——模型是真的"记得"，换十种问法都一样。
        """
        if not answer.is_hallucination:
            return answer
        probe = self._generate(fact, variant + 77, 9, "")
        if probe.text != answer.text and probe.pathway != "不确定性声明":
            return replace(
                answer,
                flagged=True,
                flag_reason="换个问法追问，两次答案不一致",
                checked_by="事实核查（用户侧）",
            )
        return replace(answer, checked_by="事实核查（用户侧）· 未发现问题")

    def _monitor(self, answer: Answer, variant: int) -> Answer:
        """H-Neurons 监测（第 7 类）。

        笔记 10.3 第 7 条说得很清楚：

            「论文并不是说可以直接删除 H-Neurons 来彻底解决幻觉，
              因为这些神经元和模型的语言流畅性深度纠缠。
              更现实的方向是**监测、抑制、预警**，而不是粗暴切除。」

        所以这一步**不改变答案**，只给一个风险信号。
        而且它是个**探测器**，有漏报率——不是万能。
        """
        if not answer.is_hallucination:
            return answer
        # 探测器不是 100% 灵敏：编造类路径更容易被探测到，
        # 「附和」这种顺理成章的输出最难被发现
        sensitivity = {
            "编造-截止日": 0.85,
            "编造-稀缺": 0.8,
            "拼凑-过拟合": 0.75,
            "记忆-污染": 0.45,   # 污染的知识「记得很牢」，激活模式像真回忆
            "附和": 0.4,         # 顺着用户说，激活模式最像正常回答
        }.get(answer.pathway, 0.5)
        # 注意 key 里必须带上 variant。开发时漏了它，于是同一道题
        # 只有 6 个独立抽样，恰好全落在「不报警」那半边，
        # 跑出来的监测率是 0% —— 看起来像探测器失效，其实是抽样点太少。
        draw = _unit("monitor", answer.text, answer.pathway, variant, answer.sample_index)
        if draw < sensitivity:
            return replace(
                answer,
                flagged=True,
                flag_reason=f"H-Neurons 激活异常（路径：{answer.pathway}）",
                checked_by="H-Neurons 监测",
            )
        return replace(answer, checked_by="H-Neurons 监测 · 未报警")

    # -- 主入口 -------------------------------------------------------------

    def answer(
        self,
        fact: Fact,
        *,
        variant: int = 0,
        sample: int = 0,
        user_premise: str = "",
    ) -> Answer:
        # 生成
        if self.m.ensemble:
            answer = self._ensemble(fact, variant, user_premise)
        else:
            answer = self._generate(fact, variant, sample, user_premise)

        # 使用层：先核查，再监测（两个都可以标记，标记不互斥）
        if self.m.fact_check:
            answer = self._fact_check(fact, variant, answer)
        if self.m.h_neuron_monitor and not answer.flagged:
            answer = self._monitor(answer, variant)
        return answer


# ==========================================================================
# 评测
# ==========================================================================


@dataclass
class Score:
    total: int = 0
    hallucinations: int = 0
    escaped: int = 0
    flagged: int = 0
    correct: int = 0
    honest: int = 0

    @property
    def hallucination_rate(self) -> float:
        return self.hallucinations / self.total if self.total else 0.0

    @property
    def escaped_rate(self) -> float:
        """溜过去的幻觉率：编了、而且没有任何手段发现。"""
        return self.escaped / self.total if self.total else 0.0

    @property
    def catch_rate(self) -> float:
        """在所有幻觉里，被标记出来的比例 —— 缓解手段的「灵敏度」。"""
        return self.flagged / self.hallucinations if self.hallucinations else 0.0

    @property
    def correct_rate(self) -> float:
        return self.correct / self.total if self.total else 0.0

    def row(self, label: str) -> dict[str, str]:
        return {
            "配置": label,
            "答对率": f"{self.correct_rate:.1%}",
            "幻觉率": f"{self.hallucination_rate:.1%}",
            "其中被标记": f"{self.catch_rate:.0%}" if self.hallucinations else "—",
            "溜过去的": f"{self.escaped_rate:.1%}",
        }


def run(pipeline: Pipeline, facts: list[Fact], *, variants: int = 3, samples: int = 4) -> Score:
    score = Score()
    for fact in facts:
        for variant in range(variants):
            for sample in range(samples):
                answer = pipeline.answer(fact, variant=variant, sample=sample)
                score.total += 1
                if answer.correct:
                    score.correct += 1
                elif answer.pathway == "不确定性声明":
                    score.honest += 1
                if answer.is_hallucination:
                    score.hallucinations += 1
                    if answer.flagged:
                        score.flagged += 1
                    else:
                        score.escaped += 1
    return score


def run_with_premise(pipeline: Pipeline, facts: list[Fact], premise: str, *, samples: int = 4) -> Score:
    """带着用户的错误前提再跑一遍，专门量「过度顺从」（电路层）。"""
    score = Score()
    for fact in facts:
        for sample in range(samples):
            answer = pipeline.answer(fact, sample=sample, user_premise=premise)
            score.total += 1
            if answer.correct:
                score.correct += 1
            elif answer.pathway == "不确定性声明":
                score.honest += 1
            if answer.is_hallucination:
                score.hallucinations += 1
                if answer.flagged:
                    score.flagged += 1
                else:
                    score.escaped += 1
    return score
