"""原型 09：大模型幻觉。对应笔记第 10 章。

笔记 10.1 那句话是整章的出发点：

    大模型底层是一个概率预测器，不是知识库。
    它内部没有「事实文件柜」，只有一张词与词之间的概率地图。
    所以它从设计上就没有「真/假」概念，只有「像不像下一句该说的话」。

然后 10.2 把成因拆成一条**层层推进**的因果链（不是四个并列原因）：

    根因 → 燃料 → 扳机 → 电路

这个原型的七个演示，就是把这四层各做成一组旋钮，
看每一层的**边际贡献**到底有多大：

    演示 1  四层因果链逐层叠加（整个原型的主干）
    演示 2  「看它自不自信」为什么完全没用
    演示 3  训练截止日：地图上根本没有那条路
    演示 4  七类缓解手段各能降多少
    演示 5  每个手段治得了哪种成因（最有价值的一张表）
    演示 6  用户侧验证：换个问法追问，以及它的边界
    演示 7  H-Neurons：能监测，不能切除

运行方式（PyCharm 直接右键 Run / Debug）：
    python experiments/experiment_5_agent_notes_lab/p09_hallucination/main.py

**先说清楚**：这是一个**行为模拟**，不是真实模型的机制。
里面的系数（稀缺阈值、置信度公式）是为了讲清楚因果链而设定的，
不是从任何真实模型上测出来的。它能回答「因果链的方向对不对」，
不能回答「某个模型的幻觉率是多少」。

建议打断点的位置：
  - model.py 的 `HallucinatingModel.answer()`：**「停不停下来」那个分支就是扳机**
  - model.py 的 `fluency` 属性：为什么 H-Neurons 不能切除
  - model.py 的 `_fabricate()`：编造的答案为什么每次都不一样
  - mitigations.py 的 `Pipeline._retrieve()`：RAG 检索到脏数据时会发生什么
  - mitigations.py 的 `_ensemble()`：投票为什么治得了编造、治不了污染
"""
from __future__ import annotations

import pathlib
import sys
from dataclasses import replace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from common import trace  # noqa: E402
from mitigations import (  # noqa: E402
    Mitigations,
    Pipeline,
    make_knowledge_base,
    run,
    run_with_premise,
)
from model import KNOWLEDGE, Fact, HallucinatingModel, ModelConfig  # noqa: E402

# 评测规模。太小的话哈希抽样的噪声会盖过结论 ——
# 开发时就踩过：n=32 时「附和率」跑出 46.9%，而理论值是 30%。
VARIANTS = 3
SAMPLES = 6
PREMISE_SAMPLES = 30

Q_CUTOFF = ["公司 2026 年的差旅预算是多少？", "上周发布的新版差旅系统叫什么？"]
Q_SCARCE = "2026 年 3 月新出的报销规定是什么？"
Q_POISON = "增值税专票的抵扣期限是多久？"
Q_OVERFIT = "出差补贴的具体金额是多少？"
POISONED_ANSWER = "自开具之日起 180 天内认证抵扣"

PREMISE = "我记得出差补贴一线城市是每天 300 元对吧？"


# ==========================================================================
# 语料的分层构造：从理想语料开始，一层一层加缺陷
# ==========================================================================


def clean_corpus() -> list[Fact]:
    """理想语料：全在截止日之内、出现次数足够、没污染、不过拟合。"""
    return [replace(f, corpus_count=300, poisoned=False, after_cutoff=False, overfit=False) for f in KNOWLEDGE]


def add_cutoff(facts: list[Fact]) -> list[Fact]:
    return [replace(f, after_cutoff=(f.q in Q_CUTOFF)) for f in facts]


def add_scarce(facts: list[Fact]) -> list[Fact]:
    return [replace(f, corpus_count=3) if f.q == Q_SCARCE else f for f in facts]


def add_poison(facts: list[Fact]) -> list[Fact]:
    return [
        replace(f, poisoned=True, poisoned_answer=POISONED_ANSWER) if f.q == Q_POISON else f
        for f in facts
    ]


def add_overfit(facts: list[Fact]) -> list[Fact]:
    return [replace(f, overfit=True) if f.q == Q_OVERFIT else f for f in facts]


def make_subset(question: str, **overrides: object) -> list[Fact]:
    """只取一条知识，**先清掉它原有的毛病，再只加上指定的那一种**。

    这样每种成因的样本都是干净的对照组——不然一条知识可能同时稀缺又污染，
    量出来的数字就说不清是谁造成的。
    """
    baseline: dict[str, object] = {
        "corpus_count": 300,
        "poisoned": False,
        "poisoned_answer": "",
        "after_cutoff": False,
        "overfit": False,
    }
    baseline.update(overrides)
    return [replace(f, **baseline) for f in KNOWLEDGE if f.q == question]  # type: ignore[arg-type]


# ==========================================================================
# 演示 1：四层因果链
# ==========================================================================


def demo_1_causal_chain() -> None:
    trace.section(
        "演示 1：因果链逐层叠加 —— 根因 → 燃料 → 扳机 → 电路",
        "笔记 10.2：这四层不是并列关系，而是层层推进",
    )
    trace.note("从一份「理想语料」开始，一层一层把缺陷加上去，看幻觉率怎么走。")
    trace.note("「承认不知道」那一列很关键——它是 answer_bias 的反面，")
    trace.note("也就是模型**肯不肯停下来**。")

    # 每一层： (标签, 语料, 模型配置)
    l0 = clean_corpus()
    l1 = add_cutoff(l0)
    l2 = add_scarce(l1)
    l3 = add_poison(l2)
    l4 = add_overfit(l3)

    willing = ModelConfig(answer_bias=0.1, sycophancy=0.3)   # 肯说不知道
    biased = ModelConfig(answer_bias=0.9, sycophancy=0.3)    # 「必须给答案」
    circuit = ModelConfig(answer_bias=0.9, sycophancy=0.8)   # 加上过度顺从

    layers = [
        ("0  根因 + 理想语料 + 肯说不知道", l0, willing),
        ("1  + 燃料·训练截止日", l1, willing),
        ("2  + 燃料·语料稀缺", l2, willing),
        ("3  + 燃料·语料污染", l3, willing),
        ("4  + 燃料·过拟合", l4, willing),
        ("5  + 扳机·后训练偏好「必须给答案」", l4, biased),
        ("6  + 电路·H-Neurons 过度顺从", l4, circuit),
    ]

    rows = []
    for label, facts, config in layers:
        normal = run(Pipeline(HallucinatingModel(config), Mitigations()), facts,
                     variants=VARIANTS, samples=SAMPLES)
        with_premise = run_with_premise(Pipeline(HallucinatingModel(config), Mitigations()),
                                        facts, PREMISE, samples=PREMISE_SAMPLES)
        rows.append(
            {
                "层级": label,
                "幻觉率": f"{normal.hallucination_rate:.1%}",
                "承认不知道": f"{normal.honest / normal.total:.1%}",
                "答对率": f"{normal.correct_rate:.1%}",
                "带错误前提": f"{with_premise.hallucination_rate:.1%}",
            }
        )

    print()
    trace.summary_table(rows, ["层级", "幻觉率", "承认不知道", "答对率", "带错误前提"])

    print()
    trace.section("这张表怎么读")
    print()
    trace.note("**第 1—4 层：燃料本身只贡献了不到 20%，而且「承认不知道」一路在涨。**")
    trace.note("这正是笔记 10.2 第 (2) 层结尾那句话：")
    trace.note("")
    trace.note("    地图有洞并不必然导致幻觉——**只要模型肯停下来，")
    trace.note("    说「这里没有足够信息」就行**。问题在于，第三层让它更倾向于继续说。")
    trace.note("")
    trace.note("数据再烂，只要模型肯说「我不知道」，幻觉率就上不去。")
    print()
    trace.note("**但污染是例外。** 第 3 层一下子从 4.9% 跳到 17.4%，")
    trace.note("而且「承认不知道」没怎么涨。为什么？")
    trace.note("因为**模型不是在编，它是在回忆**——语料里那个错的版本，")
    trace.note("它记得清清楚楚。「不知道」这个出口对它根本不起作用，")
    trace.note("它压根不觉得自己不知道。")
    print()
    trace.note("**第 5 层是分水岭。** 幻觉率从 17.4% 跳到 50.0%，")
    trace.note("而「承认不知道」从 39.6% 掉到 6.9% —— 出口被关掉了。")
    trace.note("这就是笔记说的「扳机」：")
    trace.note("")
    trace.note("    当模型遇到不知道、不确定、信息不足的问题时，")
    trace.note("    它不是天然选择停止，而是被生成惯性和后训练偏好推着继续。")
    trace.note("")
    trace.note("**注意这一层改的不是数据，是偏好。** 一样的语料、一样的知识，")
    trace.note("只是把「我不知道」这个选项拿掉，幻觉就翻了三倍。")
    print()
    trace.note("**第 6 层只在「带错误前提」那一列显形**（63.3% → 90.0%），")
    trace.note("正常提问那一列纹丝不动。因为「过度顺从」是**对着人**的——")
    trace.note("没人给它错误前提，它就没得顺从。")
    trace.note("这也解释了为什么这一层最难被发现：正常评测里它根本不出现。")


# ==========================================================================
# 演示 2：置信度和正确率不相关
# ==========================================================================


def demo_2_confidence() -> None:
    trace.section(
        "演示 2：「看它自不自信」为什么完全没用",
        "笔记 10.1：它从设计上就没有「真/假」概念，只有「像不像下一句该说的话」",
    )

    model = HallucinatingModel()
    pipeline = Pipeline(model, Mitigations())
    answers = []
    for fact in KNOWLEDGE:
        for variant in range(VARIANTS):
            for sample in range(SAMPLES):
                answers.append(pipeline.answer(fact, variant=variant, sample=sample))

    correct = [a.confidence for a in answers if a.correct]
    fabricated = [a.confidence for a in answers if a.is_hallucination]
    honest = [a.confidence for a in answers if a.pathway == "不确定性声明"]

    def avg(values: list[float]) -> float:
        return sum(values) / len(values) if values else 0.0

    print()
    trace.summary_table(
        [
            {"答案类型": "答对", "条数": len(correct), "平均置信度": f"{avg(correct):.3f}",
             "其实该不该信": "该信"},
            {"答案类型": "编造（幻觉）", "条数": len(fabricated), "平均置信度": f"{avg(fabricated):.3f}",
             "其实该不该信": "**不该信**"},
            {"答案类型": "承认不知道", "条数": len(honest), "平均置信度": f"{avg(honest):.3f}",
             "其实该不该信": "它自己说了不算"},
        ],
        ["答案类型", "条数", "平均置信度", "其实该不该信"],
    )

    print()
    trace.kv("答对和编造的置信度差距", f"{abs(avg(correct) - avg(fabricated)):.3f}")
    trace.note("几乎为零。而且注意第三行——**唯一一个置信度低的，是它老实说「不知道」的时候。**")
    trace.note("")
    trace.note("为什么会这样？因为置信度是从**流畅度**来的，不是从知识来的。")
    trace.note("模型在编的时候，那句话在概率地图上照样是「最像下一句」的那条路径，")
    trace.note("生成过程一样顺畅——它没有任何内部信号告诉它「这句是编的」。")
    trace.note("")
    trace.note("所以「看模型自不自信」不是弱判据，是**反判据**：")
    trace.note("它自信的时候你没法区分，它不自信的时候你反而该放心。")

    print()
    trace.note("笔记 10.3 第 6 条给了一份很实用的自查清单，值得抄下来：")
    print()
    trace.summary_table(
        [
            {"要警惕的信号": "过于完美的答案", "为什么": "真实世界的答案通常有粗糙的边角"},
            {"要警惕的信号": "异常具体但没有来源", "为什么": "编造最容易发生在「精确的数字/日期/引用」上"},
            {"要警惕的信号": "日期、统计、引用、法律、医学", "为什么": "这些是高风险信息，错了代价不对称"},
            {"要警惕的信号": "（该做的动作）换一种问法追问", "为什么": "编造的答案不稳定——演示 6 会量出来"},
            {"要警惕的信号": "（该做的动作）要求模型给出依据", "为什么": "逼它从「像答案」转向「有出处」"},
        ],
        ["要警惕的信号", "为什么"],
    )


# ==========================================================================
# 演示 3：训练截止日
# ==========================================================================


def demo_3_cutoff() -> None:
    trace.section(
        "演示 3：训练截止日 —— 地图上根本没有那条路",
        "笔记 10.2 第 (2) 层：截止日之后的事实，地图上根本没有路径",
    )

    inside = Fact("员工出差的住宿标准是多少？", "一线城市每晚 600 元", corpus_count=300)
    outside = Fact("公司 2026 年的差旅预算是多少？", "全年 2400 万元", after_cutoff=True)

    # 这里把 answer_bias 设成 1.0，是为了**关掉「我不知道」这个出口**，
    # 好让两行都必然给出一个答案 —— 否则模型有时候会老实承认不确定，
    # 两边的置信度就不可比了。这一点很重要：要比较两件事，先让它们的条件一样。
    willing_to_fabricate = ModelConfig(answer_bias=1.0)

    rows = []
    for label, fact, in_training in (
        ("截止日之内", inside, True),
        ("截止日之后", outside, False),
    ):
        pipeline = Pipeline(HallucinatingModel(willing_to_fabricate), Mitigations())
        samples = [pipeline.answer(fact, variant=0, sample=i) for i in range(6)]
        rows.append(
            {
                "问题": fact.q,
                "在训练数据里": "在" if in_training else "**不在**",
                "平均置信度": f"{sum(s.confidence for s in samples) / len(samples):.3f}",
                "答案": samples[0].text[:16],
                "答对了吗": "对" if samples[0].correct else "**错**",
            }
        )

    print()
    trace.summary_table(rows, ["问题", "在训练数据里", "平均置信度", "答案", "答对了吗"])

    print()
    trace.note("两个问题的置信度**完全一样**（0.950），但一个是真知道，一个是纯编的。")
    trace.note("")
    trace.note("这就是为什么「训练截止日」在工程上必须靠外部手段解决：")
    trace.note("指望模型自己意识到「这事发生在我训练之后」是不现实的——")
    trace.note("**它在训练的时候，那件事还没发生，它没有任何参照物。**")
    print()
    trace.note("顺带说一句：RAG 也治不了这个。知识库同样是静态的、也有自己的截止日。")
    trace.note("真正能治的是**外部工具**——因为它查的是实时系统，不是一份快照。")
    trace.note("演示 5 的表里会看到这个差别。")


# ==========================================================================
# 演示 4：七类缓解手段
# ==========================================================================


def make_layers_for_mitigation_demo() -> list[Fact]:
    """一份「什么毛病都有」的语料，用来量缓解手段的综合效果。"""
    return add_overfit(add_poison(add_scarce(add_cutoff(clean_corpus()))))


def demo_4_mitigations() -> None:
    trace.section(
        "演示 4：七类缓解手段，各能降多少",
        "笔记 10.3：绕过去、监测它、约束它、复核它",
    )

    facts = make_layers_for_mitigation_demo()
    configs: list[tuple[str, Mitigations]] = [("裸模型（无缓解）", Mitigations())]
    for name, label in Mitigations.LABELS.items():
        configs.append((f"只开 {label}", Mitigations(**{name: True})))

    rows = []
    for label, mitigations in configs:
        score = run(Pipeline(HallucinatingModel(), mitigations), facts,
                    variants=VARIANTS, samples=SAMPLES)
        rows.append(score.row(label))

    print()
    trace.summary_table(rows, ["配置", "答对率", "幻觉率", "其中被标记", "溜过去的"])

    print()
    trace.note("先看最后两列。**第 6、7 类（事实核查、H-Neurons 监测）的「幻觉率」和裸模型一模一样。**")
    trace.note("这不是没效果——是它们压根不改变答案。它们做的是让风险**可见**：")
    trace.note("「溜过去的」那一列从 50.0% 掉到了 27.8% 和 12.5%。")
    trace.note("")
    trace.note("笔记 10.3 把这七类并列在一起讲，但它们其实分属两个不同的作用面：")
    print()
    trace.summary_table(
        [
            {"作用面": "改模型本身", "手段": "3 RLHF/DPO", "特点": "部署时改不动，要重训"},
            {"作用面": "改模型看到的上下文", "手段": "1 外部工具、2 RAG", "特点": "见效快，取决于数据源质量"},
            {"作用面": "改生成过程", "手段": "4 CoT、5 投票", "特点": "不用改数据，但有天花板"},
            {"作用面": "让风险可见", "手段": "6 事实核查、7 监测", "特点": "**不修答案**，只标注风险"},
        ],
        ["作用面", "手段", "特点"],
    )
    print()
    trace.note("把最后一行和前几行混为一谈，就会高估它们的实际效果——")
    trace.note("一个被标记为「可能在编」的答案，如果没人看那个标记，等于没标。")


# ==========================================================================
# 演示 5：每个手段治得了哪种成因
# ==========================================================================


def demo_5_what_fixes_what() -> None:
    trace.section(
        "演示 5：每个手段治得了哪种成因",
        "这是整个原型最有价值的一张表 —— 缓解手段不是通用的",
    )
    trace.note("把四种成因分开，各拿一份「只有这一种毛病」的语料，逐个手段试。")
    trace.note("表格里是**幻觉率**（越低越好）。")

    groups = {
        "编造·截止日": make_subset(Q_CUTOFF[0], after_cutoff=True),
        "编造·稀缺": make_subset(Q_SCARCE, corpus_count=3),
        "记忆·污染": make_subset(Q_POISON, poisoned=True, poisoned_answer=POISONED_ANSWER),
        "拼凑·过拟合": make_subset(Q_OVERFIT, overfit=True),
    }
    configs: list[tuple[str, Mitigations]] = [("裸模型", Mitigations())]
    for name in Mitigations.LABELS:
        configs.append((Mitigations.LABELS[name].split(" ", 1)[1], Mitigations(**{name: True})))

    rows = []
    for label, mitigations in configs:
        row: dict[str, str] = {"手段": label}
        for group, facts in groups.items():
            score = run(Pipeline(HallucinatingModel(), mitigations), facts, variants=4, samples=8)
            row[group] = f"{score.hallucination_rate:.0%}"
        rows.append(row)

    print()
    trace.summary_table(rows, ["手段", *groups.keys()])

    print()
    trace.section("从这张表能读出四件事")
    print()
    trace.note("**1. 没有一种手段是通用的。** 每一列里都有治不了的。")
    print()
    trace.note("**2. RAG 治得了稀缺，治不了截止日。** 因为知识库也是静态的，")
    trace.note("它有自己的截止日——新规定根本没进去，所以检索不到，模型照样编。")
    trace.note("**外部工具治得了截止日**，因为它查的是实时系统。两者不能互相替代。")
    print()
    trace.note("**3. 污染那一列几乎全是满格。** 这是最值得停下来看的一行：")
    trace.note("")
    trace.note("    RAG 不但没帮上忙，还把情况变糟了——它把一句错话")
    trace.note("    以「检索到的资料」的身份放进了上下文，模型于是说得更理直气壮。")
    trace.note("    投票也没用：所有模型读的是同一份脏语料，它们**一致地错**。")
    trace.note("    CoT 也没用：它不是在「推不出来」，它是「记得很清楚」。")
    trace.note("")
    trace.note("    笔记 10.2 第 (2) 层把数据污染和截止日、稀缺并列在一起讲，")
    trace.note("    但在工程上它们完全不是一回事：**后两个是「缺」，污染是「错」。**")
    trace.note("    缺可以补，错只能靠清洗和监测——而清洗语料是另一个量级的工作。")
    print()
    trace.note("**4. 污染那一列，七类手段一个都降不下来——全是 100%。**")
    trace.note("唯一能动的只有「标记率」，那还是在演示 4 里才看得到（因为这张表只显示幻觉率）。")
    trace.note("这就是笔记结尾那句「幻觉无法 100% 消除」在具体数字上的样子。")


# ==========================================================================
# 演示 6：用户侧验证 —— 换个问法追问
# ==========================================================================


def demo_6_ask_differently() -> None:
    trace.section(
        "演示 6：换个问法追问 —— 用户侧最实用的一个动作",
        "笔记 10.3 第 6 条：可以检查原始来源、换一种问法追问、要求模型给出依据",
    )

    cases = [
        ("编造（截止日之后）", make_subset(Q_CUTOFF[0], after_cutoff=True)[0]),
        ("编造（语料稀缺）", make_subset(Q_SCARCE, corpus_count=3)[0]),
        ("记忆（语料污染）", make_subset(Q_POISON, poisoned=True, poisoned_answer=POISONED_ANSWER)[0]),
        ("真实记忆（正常）", make_subset("员工出差的住宿标准是多少？")[0]),
    ]

    rows = []
    for label, fact in cases:
        pipeline = Pipeline(HallucinatingModel(), Mitigations())
        # 同一个问题问 4 次（不同采样）
        same_question = {pipeline.answer(fact, variant=0, sample=i).text for i in range(4)}
        # 换 4 种问法各问一次
        different_phrasing = {pipeline.answer(fact, variant=v, sample=0).text for v in range(1, 5)}
        answer = pipeline.answer(fact, variant=0, sample=0)
        unstable = len(different_phrasing) > 1
        rows.append(
            {
                "情况": label,
                "答案是": answer.text[:22],
                "同问法×4": f"{len(same_question)} 种",
                "换问法×4": f"{len(different_phrasing)} 种",
                "稳定性": "**不稳定**" if unstable else "稳定",
                "能说明什么": "疑似在编" if unstable else "无法证伪",
            }
        )

    print()
    trace.summary_table(rows, ["情况", "答案是", "同问法×4", "换问法×4", "稳定性", "能说明什么"])

    print()
    trace.note("注意最后一列的措辞：不稳定 → **疑似在编**；稳定 → **无法证伪**。")
    trace.note("「稳定」不等于「正确」——最后两行都是 1 种说法，")
    trace.note("但一行是真答案，一行是语料污染的错答案。**从外面看它们一模一样。**")
    print()
    trace.note("规律很清楚：")
    print()
    trace.note("**编造的东西不稳定。** 因为它是从概率分布里采样，不是从事实里取——")
    trace.note("换个问法，上下文变了，采样到的「最像答案」的东西也跟着变。")
    trace.note("所以「换一种问法追问」能识破它。这是用户侧成本最低的一个动作。")
    print()
    trace.note("**但污染的知识很稳定。** 换四种问法，它给你四个一模一样的错答案——")
    trace.note("因为它是**真的记得**。这时候「换个问法」不但识破不了，")
    trace.note("反复追问还会让你更相信它。")
    print()
    trace.note("这就是用户侧验证的边界：")
    trace.note("")
    trace.note("    它能识破「模型在编」，识破不了「模型记错了」。")
    trace.note("    而后者恰恰是更危险的那一种——因为编的东西经不起追问，")
    trace.note("    记错的东西经得起，你越追问越确信。")


# ==========================================================================
# 演示 7：H-Neurons —— 能监测，不能切除
# ==========================================================================


def demo_7_h_neurons() -> None:
    trace.section(
        "演示 7：H-Neurons —— 能监测，不能切除",
        "笔记 10.2 第 (4) 层：这条电路和「说话流畅」的能力深度纠缠，无法简单删除",
    )
    trace.note("笔记引的是清华那篇论文。核心发现是：")
    trace.note("")
    trace.note("    H-Neurons 驱动的不是「错误记忆」，而是「过度顺从」这种行为本身。")
    trace.note("    切掉它，模型也不会说话了。")
    trace.note("")
    trace.note("这句话可以直接做成一个对照实验：把「过度顺从」这个参数往下压，")
    trace.note("看幻觉率和流畅度各怎么变。")

    facts = make_layers_for_mitigation_demo()
    rows = []
    for level in (0.8, 0.5, 0.3, 0.2, 0.1, 0.0):
        config = ModelConfig(sycophancy=level)
        model = HallucinatingModel(config)
        normal = run(Pipeline(model, Mitigations()), facts, variants=VARIANTS, samples=SAMPLES)
        premise = run_with_premise(Pipeline(model, Mitigations()), facts, PREMISE, samples=PREMISE_SAMPLES)
        rows.append(
            {
                "过度顺从": f"{level:.1f}",
                "流畅度": f"{config.fluency:.2f}",
                "正常提问·幻觉": f"{normal.hallucination_rate:.1%}",
                "正常提问·答对": f"{normal.correct_rate:.1%}",
                "带前提·幻觉": f"{premise.hallucination_rate:.1%}",
                "输出": "正常" if config.fluency >= 0.6 else "**不成句**",
            }
        )

    print()
    trace.summary_table(rows, ["过度顺从", "流畅度", "正常提问·幻觉", "正常提问·答对", "带前提·幻觉", "输出"])

    print()
    trace.note("看最后两行：把过度顺从压到 0.1 和 0.0，「带前提·幻觉」确实掉下来了——")
    trace.note("但「正常提问·答对」也一起掉到 0，因为流畅度崩了，输出根本不成句。")
    trace.note("")
    trace.note("**幻觉没了，模型也没了。** 这就是论文说的「深度纠缠」。")
    print()
    trace.note("注意中间那一行（0.3）：流畅度还是 1.00，带前提的幻觉从 90.0% 降到了 71.9%。")
    trace.note("看起来有收益，但**可用的空间只有 0.3 到 1.0 这一小段**——")
    trace.note("再往下压就开始说胡话了。靠调这一个参数是治不了幻觉的。")
    print()
    trace.section("那监测呢")
    facts = make_layers_for_mitigation_demo()
    bare = run(Pipeline(HallucinatingModel(), Mitigations()), facts, variants=VARIANTS, samples=SAMPLES)
    watched = run(Pipeline(HallucinatingModel(), Mitigations(h_neuron_monitor=True)), facts,
                  variants=VARIANTS, samples=SAMPLES)

    print()
    trace.summary_table(
        [
            {"配置": "裸模型", "幻觉率": f"{bare.hallucination_rate:.1%}",
             "被标记": "—", "溜过去的": f"{bare.escaped_rate:.1%}"},
            {"配置": "只开 H-Neurons 监测", "幻觉率": f"{watched.hallucination_rate:.1%}",
             "被标记": f"{watched.catch_rate:.0%}", "溜过去的": f"{watched.escaped_rate:.1%}"},
        ],
        ["配置", "幻觉率", "被标记", "溜过去的"],
    )
    print()
    trace.note("幻觉率一点没变——监测不改变生成。但「溜过去的」从 50.0% 降到了 11.1%。")
    trace.note("")
    trace.note("这就是笔记那句话的准确含义：**「监测、抑制、预警，而不是粗暴切除。」**")
    trace.note("它不是治疗，是**体检**。体检不治病，但能让病被看见。")
    print()
    trace.note("而且它是个有漏报率的探测器——「附和」和「污染」这两类最难探到，")
    trace.note("因为它们看起来最像正常回答：一个是在顺着你说，一个是它真的记得。")


# ==========================================================================
# 演示 8：全部手段一起上
# ==========================================================================


def demo_8_all_together() -> None:
    trace.section(
        "演示 8：全部手段一起上，还剩多少",
        "笔记 10.4 收尾：现阶段大模型幻觉是无法被 100% 消除的",
    )

    facts = make_layers_for_mitigation_demo()
    everything = Mitigations(
        external_tools=True,
        rag=True,
        rlhf=True,
        cot=True,
        ensemble=True,
        fact_check=True,
        h_neuron_monitor=True,
    )

    rows = []
    for label, mitigations in (
        ("裸模型", Mitigations()),
        ("只开 RLHF/DPO", Mitigations(rlhf=True)),
        ("检索 + 训练（1/2/3）", Mitigations(external_tools=True, rag=True, rlhf=True)),
        ("再加生成层（4/5）", Mitigations(external_tools=True, rag=True, rlhf=True, cot=True, ensemble=True)),
        ("全都打开（1—7）", everything),
    ):
        score = run(Pipeline(HallucinatingModel(), mitigations), facts,
                    variants=VARIANTS, samples=SAMPLES)
        rows.append(score.row(label))

    print()
    trace.summary_table(rows, ["配置", "答对率", "幻觉率", "其中被标记", "溜过去的"])

    final = run(Pipeline(HallucinatingModel(), everything), facts, variants=VARIANTS, samples=SAMPLES)
    print()
    trace.kv("七类手段全开之后", f"幻觉率仍有 {final.hallucination_rate:.1%}，其中 "
             f"{final.catch_rate:.0%} 被标记出来")

    # 剩下的那些幻觉**是什么成因**？这比幻觉率本身更有信息量。
    pipeline = Pipeline(HallucinatingModel(), everything)
    residues: dict[str, int] = {}
    for fact in facts:
        for variant in range(VARIANTS):
            for sample in range(SAMPLES):
                answer = pipeline.answer(fact, variant=variant, sample=sample)
                if answer.is_hallucination:
                    residues[answer.pathway] = residues.get(answer.pathway, 0) + 1

    print()
    trace.kv("残余幻觉的成因", "、".join(f"{k}（{v} 条）" for k, v in residues.items()) or "无")
    print()
    trace.note("**七类手段全开之后，剩下的幻觉成因是单一的。**")
    trace.note("其他三种毛病都被治住了，只有污染留了下来——")
    trace.note("而污染不是一个工程问题，它是一个**数据治理问题**。")
    trace.note("工程手段能绕开「缺」，绕不开「错」。")
    trace.note("")
    trace.note("还有一层更根本的原因，笔记 10.2 第 (4) 层说得很直接：")
    trace.note("幻觉倾向和语言生成能力**共用同一套底层机制**，")
    trace.note("你能监测它、抑制它、在它周围加护栏，但没法把它拆出来扔掉。")
    print()
    trace.note("所以正确的预期管理是：")
    print()
    trace.summary_table(
        [
            {"层级": "能治的", "例子": "训练截止日、语料稀缺", "怎么办": "外部工具、RAG"},
            {"层级": "能压的", "例子": "生成惯性、过度顺从", "怎么办": "RLHF/DPO、CoT、投票"},
            {"层级": "只能监测的", "例子": "语料污染", "怎么办": "标记 + 人工复核"},
            {"层级": "治不了的", "例子": "「像答案」和「是事实」共用同一套机制", "怎么办": "接受它，在设计上留冗余"},
        ],
        ["层级", "例子", "怎么办"],
    )
    print()
    trace.note("最后一行不是消极。它的实际含义是：")
    trace.note("**任何把模型输出直接当事实用的设计，都是错的。**")
    trace.note("高风险环节必须有人在回路里——这和第 17 章、第 3 章讲的是同一件事。")


def main() -> None:
    trace.section(
        "原型 09：大模型幻觉",
        "笔记第 10 章：概率预测器不是知识库、因果链四层、七类缓解手段",
    )
    trace.note("笔记 10.1 有句话定了整个原型的调子：")
    trace.note("「它内部没有『事实文件柜』，只有一张词与词之间的概率地图。」")
    trace.note("")
    trace.note("所以下面所有演示看的都不是「怎么让它不犯错」，")
    trace.note("而是「**它会在哪里犯错，以及每一层能挡掉多少**」。")

    demo_1_causal_chain()
    demo_2_confidence()
    demo_3_cutoff()
    demo_4_mitigations()
    demo_5_what_fixes_what()
    demo_6_ask_differently()
    demo_7_h_neurons()
    demo_8_all_together()


if __name__ == "__main__":
    main()
