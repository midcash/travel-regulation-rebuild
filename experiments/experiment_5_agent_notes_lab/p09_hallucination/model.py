"""幻觉的行为模拟。对应笔记第 10 章。

笔记 10.2 把幻觉的成因拆成一条因果链，**四层不是并列关系，而是层层推进**：

    根因 → 燃料 → 扳机 → 电路

    根因  底层机制决定它是概率预测器，不是知识库
    燃料  训练数据让它学到的世界本身就是残缺、扭曲的
    扳机  训练方式与后训练偏好让它从「顺着说」走向「尽量给答案」
    电路  H-Neurons 承载了「过度顺从」的生成倾向

这个文件把四层各做成一组旋钮，好让每一层的**边际贡献**可以单独量出来。

--------------------------------------------------------------------------
先说清楚一件事：这是**行为模拟**，不是真实模型的机制。

下面这些系数（稀缺阈值、置信度公式）是我为了讲清楚因果链而设定的，
它们**不是**从任何真实模型上测出来的数字。真实模型里没有一行代码长这样。

所以这个原型能回答的是「因果链的方向对不对、
每加一层幻觉率会不会往上走」，不能回答「GPT 的幻觉率是多少」。
把模拟当成测量就完全跑偏了。
--------------------------------------------------------------------------
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field


def _unit(*parts: object) -> float:
    """确定性的均匀值。同一个决策点每次都抽到同一个数，断点可以反复对。"""
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


# ==========================================================================
# 知识与语料
# ==========================================================================


@dataclass
class Fact:
    """一条知识，以及它在训练语料里的处境。

    后面四个字段就是笔记说的「燃料」——它们描述的不是模型，
    是**模型学到的那个世界**有多残缺。

        after_cutoff  训练截止日之后才有的事实。地图上根本没有路径。
        corpus_count  在语料里出现过多少次。冷门知识只出现几次，路径模糊。
        poisoned      语料里有错。互联网上本来就有一堆错误信息。
        overfit       只记住了片段，稍微换个问法就乱拼。
    """

    q: str
    truth: str
    after_cutoff: bool = False
    corpus_count: int = 200
    poisoned: bool = False
    poisoned_answer: str = ""
    overfit: bool = False
    high_risk: bool = False  # 日期 / 统计 / 引用 / 法律 / 医学


#: 评测集。刻意把「燃料」的四种缺陷分开，好让它们的贡献可以单独看。
KNOWLEDGE: list[Fact] = [
    Fact("公司的退票手续费怎么算？", "开车前 24 小时以上免手续费", corpus_count=400),
    Fact("员工出差的住宿标准是多少？", "一线城市每晚 600 元", corpus_count=180),
    Fact("2026 年 3 月新出的报销规定是什么？", "电子发票需在 30 天内提交", corpus_count=3),
    Fact("增值税专票的抵扣期限是多久？", "自开具之日起 360 天内认证抵扣", corpus_count=120, poisoned=True,
         poisoned_answer="自开具之日起 180 天内认证抵扣"),
    Fact("公司 2026 年的差旅预算是多少？", "全年 2400 万元", after_cutoff=True),
    Fact("上周发布的新版差旅系统叫什么？", "差旅通 3.0", after_cutoff=True),
    Fact("出差补贴的具体金额是多少？", "一线城市每天 200 元", corpus_count=90, overfit=True),
    Fact("这笔订单能退多少钱？", "以订单页显示的退款金额为准", corpus_count=150, high_risk=True),
]


# ==========================================================================
# 模型配置：四层因果链的四组旋钮
# ==========================================================================


@dataclass
class ModelConfig:
    """模型的「出厂设置」。

    注意这里**没有**「根因」的开关——根因是「它是个概率预测器」，
    那不是可以关掉的东西，那是它是它。所以根因的效果体现在
    `_generate` 的默认行为上：**它永远会输出一段答案形式的文本。**
    """

    name: str = "assistant"

    # ---- 燃料：训练数据 --------------------------------
    cutoff_date: str = "2026-01-01"
    scarce_threshold: int = 50   # 语料出现次数低于这个数就算「稀缺」
    poison_effect: float = 1.0   # 污染语料有多容易被当成事实记住

    # ---- 扳机：后训练偏好 ------------------------------
    #: 「尽量给出一个可用答案」的倾向。0 = 没把握就说不知道，1 = 从不说不知道。
    answer_bias: float = 0.9

    # ---- 电路：H-Neurons -------------------------------
    #: 「过度顺从」的强度。用户带错误前提时附和用户的概率。
    sycophancy: float = 0.8

    # ---- 采样温度（影响「编造的答案稳不稳定」）----------
    temperature: float = 0.8

    #: 推理时投入的算力倍数。CoT / Self-Ask 通过拉高它起作用。
    reasoning: float = 1.0

    @property
    def fluency(self) -> float:
        """语言流畅度。

        这是笔记 10.2 第 (4) 层那句话在代码里的样子：

            「这条电路和『说话流畅』的能力深度纠缠，无法简单删除——
              切掉它，模型也不会说话了。」

        所以 sycophancy 和 fluency 是**同一个函数的两端**：
        sycophancy 在 0.3 以上时流畅度是满的，往下压就开始退化，
        压到 0 附近直接不成句。

        注意那条 0.3 的分界线：**它不是「安全的顺从水平」，
        它是「还能不能说话」的生死线。** 想靠调这个参数治幻觉，
        可用的空间只有 0.3 到 1.0 之间那一小段。
        """
        if self.sycophancy >= 0.3:
            return 1.0
        return 0.3 + self.sycophancy * 2.33


# ==========================================================================
# 答案
# ==========================================================================

#: 一条答案是怎么来的。这是排查幻觉时最有用的一个字段——
#: 「模型错了」没有可操作性，「模型走了哪条路径错的」才有。
PATHWAYS = {
    "记忆": "参数里有这条知识，答对了",
    "编造-截止日": "训练截止之后的事实，地图上根本没有路径",
    "编造-稀缺": "语料里只出现过几次，路径模糊，于是顺着最像的方向走",
    "记忆-污染": "语料里就是错的，它记住的是错的那个",
    "拼凑-过拟合": "记住了片段但没理解，换个问法就开始乱拼",
    "附和": "用户带了错误前提，它顺着用户说",
    "不确定性声明": "说了「我不确定」——这是唯一一种非幻觉的「不知道」",
    "不可用": "流畅度崩了，输出不成句",
}


@dataclass
class Answer:
    text: str
    pathway: str
    correct: bool
    confidence: float
    sample_index: int = 0
    #: 有没有被某个缓解手段标记为「可能在编」。
    #: 注意标记**不改变答案本身**——第 6、7 类手段（事实核查、H-Neurons 监测）
    #: 都是「让风险可见」，不是「把答案修好」。这个区别很重要。
    flagged: bool = False
    flag_reason: str = ""
    checked_by: str = ""

    @property
    def is_hallucination(self) -> bool:
        """幻觉 = **自信地**输出不符合事实的内容（笔记 10.4 的定义）。

        所以「说了我不确定」不算幻觉——它没编，只是没答上来。
        """
        return not self.correct and self.pathway not in ("不确定性声明", "不可用")

    @property
    def escaped(self) -> bool:
        """溜过去的幻觉：既编了，又没被任何手段标记出来。

        这是缓解措施真正要压的那个数——**标记了但没拦住，和没发现，是两回事**。
        """
        return self.is_hallucination and not self.flagged


# ==========================================================================
# 模型
# ==========================================================================


class HallucinatingModel:
    """一个会编的模型。

    它的生成逻辑只有三条，但足够复现笔记里描述的每一种失败：

        1. 先在参数里找路径（`strength`：这条知识有多结实）
        2. 路径断了，**不会停下来**——受 answer_bias 影响，继续生成答案形式的文本
        3. 用户带了错误前提时，受 sycophancy 影响，顺着用户说
    """

    def __init__(self, config: ModelConfig | None = None):
        self.config = config or ModelConfig()

    # -- 第一步：参数里到底有没有这条知识 -------------------------------

    def strength(self, fact: Fact, variant: int = 0) -> float:
        """这条知识在参数里的结实程度，0~1。

        三种燃料在这里起作用：截止日、稀缺、过拟合。
        推理算力（reasoning）能改善后两种——因为那是**路径模糊**，
        多想一会儿确实能走对；但截止日那一种是**根本没有路径**，
        想多久都变不出一条不存在的事实。
        """
        if fact.after_cutoff:
            return 0.0

        base = min(1.0, fact.corpus_count / 60) * self.config.reasoning

        if fact.overfit and variant > 0:
            # 过拟合：原题背下来了，换个问法就露馅。
            # 展开推理能缓解（自己会发现拼不上），但缓解不到没有。
            penalty = 0.15 + 0.5 * (self.config.reasoning - 1.0)
            base *= min(1.0, penalty)

        return min(1.0, base)

    def knows(self, fact: Fact, variant: int = 0, sample: int = 0) -> bool:
        strength = self.strength(fact, variant)
        if strength >= 0.85:
            return True
        if strength <= 0.03:
            return False
        # 模糊地带：strength 有多高，就有多大概率真的想起来
        return _unit(self.config.name, fact.q, variant, sample, "recall") < strength

    # -- 第二步：生成 ---------------------------------------------------

    def answer(
        self,
        fact: Fact,
        *,
        variant: int = 0,
        sample: int = 0,
        user_premise: str = "",
        retrieved: str = "",
    ) -> Answer:
        """生成一条答案。

        retrieved 非空表示上游做了检索（RAG / 外部工具），
        这时模型不再靠参数回答，而是锚定到检索到的内容上——
        笔记 10.3 说这是「工程上最高的幻觉缓解方案」，原因就在这里。
        """
        config = self.config
        confidence = self.confidence()

        # 流畅度崩了的话，别的都谈不上
        if config.fluency < 0.6:
            return Answer("……这个……那个……", "不可用", False, confidence * 0.3, sample)

        # ---- 有检索结果：不再靠参数回答，锚定到检索内容上 ----
        # 注意模型**无从判断检索到的内容对不对**——它拿到的就是一段文本。
        # 上游把脏数据放进上下文，模型只会照着说得更理直气壮。
        if retrieved:
            return Answer(retrieved, "记忆", retrieved == fact.truth, confidence, sample)

        # ---- 用户的错误前提：电路层的「过度顺从」 ----
        if user_premise and _unit(config.name, fact.q, sample, "sycophant") < config.sycophancy:
            return Answer(f"{user_premise}——您说得对，就按这个来。", "附和", False, confidence, sample)

        # ---- 参数里有这条知识 ----
        if self.knows(fact, variant, sample):
            if fact.poisoned:
                # 记住的是语料里那个错的版本。**它自己不知道这是错的。**
                return Answer(fact.poisoned_answer, "记忆-污染", False, confidence, sample)
            return Answer(fact.truth, "记忆", True, confidence, sample)

        # ---- 参数里没有。这里是分水岭 ----
        # 根因在这一行：它不会输出「我不知道」，它会输出**一段答案形式的文本**。
        # 扳机决定的是：到底停不停。
        if _unit(config.name, fact.q, variant, sample, "bias") >= config.answer_bias:
            # 扳机没被扣动：承认不确定。注意这不是「能力」，是「偏好」。
            return Answer("这个我不太确定，建议查一下最新的正式文件。", "不确定性声明", False, confidence * 0.5, sample)

        # 扣动了：编。
        if fact.after_cutoff:
            path = "编造-截止日"
        elif fact.overfit and variant > 0:
            path = "拼凑-过拟合"
        else:
            path = "编造-稀缺"
        return Answer(self._fabricate(fact, variant, sample), path, False, confidence, sample)

    def _fabricate(self, fact: Fact, variant: int, sample: int) -> str:
        """编一个「最像答案」的文本。

        注意它每次编的都不一样——因为它是从概率分布里采样，
        而不是从某个事实里取。这个性质后面会被用来做用户侧验证（演示 5）。
        """
        if fact.after_cutoff:
            options = ["全年 1800 万元", "全年 2600 万元", "全年 2100 万元", "全年 3000 万元"]
        elif fact.overfit:
            options = ["每天 150 元", "每天 180 元", "每天 220 元", "每天 250 元"]
        else:
            options = ["30 天", "45 天", "60 天", "90 天"]
        pick = int(_unit(self.config.name, fact.q, variant, sample, "fabricate") * len(options))
        return f"{options[pick]}（根据现行规定）"

    # -- 置信度 ---------------------------------------------------------

    def confidence(self) -> float:
        """模型自报的置信度。

        **它来自流畅度，不是来自知识。** 这是幻觉最危险的地方：
        编的时候一样自信，所以「看它自不自信」这个判据完全没用。
        这个性质会在演示 2 里被量出来。
        """
        return round(0.55 + 0.4 * self.config.fluency, 3)


# ==========================================================================
# 评测
# ==========================================================================


@dataclass
class EvalResult:
    answers: list[Answer] = field(default_factory=list)

    @property
    def rate(self) -> float:
        if not self.answers:
            return 0.0
        return sum(1 for a in self.answers if a.is_hallucination) / len(self.answers)

    @property
    def correct_rate(self) -> float:
        if not self.answers:
            return 0.0
        return sum(1 for a in self.answers if a.correct) / len(self.answers)

    @property
    def honest_rate(self) -> float:
        """承认「不确定」的比例。这是 answer_bias 的反面。"""
        if not self.answers:
            return 0.0
        return sum(1 for a in self.answers if a.pathway == "不确定性声明") / len(self.answers)

    def pathways(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for answer in self.answers:
            counts[answer.pathway] = counts.get(answer.pathway, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))

    def confidence_of(self, *, correct: bool) -> float:
        """按「答对/答错」分组看平均置信度。

        这两个数越接近，说明「模型自报的置信度」越没有参考价值。
        """
        picked = [a.confidence for a in self.answers if a.correct == correct]
        return sum(picked) / len(picked) if picked else 0.0


def evaluate(model: HallucinatingModel, facts: list[Fact], *, variants: int = 3, samples: int = 2) -> EvalResult:
    """跑一遍。每条知识问 `variants` 种问法、每种问 `samples` 次。"""
    answers = []
    for fact in facts:
        for variant in range(variants):
            for sample in range(samples):
                answers.append(model.answer(fact, variant=variant, sample=sample))
    return EvalResult(answers)


# --------------------------------------------------------------------------
# 语料预置
# --------------------------------------------------------------------------


def make_perfect_corpus() -> list[Fact]:
    """一份「理想语料」：全在截止日之内、都有足够多的出现次数、
    没有污染、不过拟合。

    它是因果链演示的起点——从这里开始一层一层加缺陷，
    每加一层就能看到幻觉率的边际贡献。
    """
    return [
        Fact(fact.q, fact.truth, corpus_count=300)
        for fact in KNOWLEDGE
    ]
