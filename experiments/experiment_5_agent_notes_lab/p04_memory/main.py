"""原型 04：Agent 的记忆功能。对应笔记第 5 章（+ 11.3 / 12.3 的框架细节）。

笔记把记忆分成两层，这个原型也分成两层：

    短期记忆 = 上下文窗口（最近的对话、回复、工具结果、反思）
               窗口有限 -> 靠 Compaction 压缩成「结构化摘要 + 最近原文」

    长期记忆 = 存在运行环境之外的存储（数据库 / 知识图谱 / 向量库）
               需要时通过 memory_search 工具**按需检索**，而不是全塞进上下文

两个最容易讲不清楚的点，这里都用数字说话：
  - 压缩到底压掉了什么、又强制保留了什么？
  - "按需检索"比"全量注入"省了多少 token？

运行方式（PyCharm 直接右键 Run / Debug）：
    python experiments/experiment_5_agent_notes_lab/p04_memory/main.py

长期记忆会真实落盘到 p04_memory/workspace/，跑完可以直接打开看。

建议打断点的位置：
  - context_window.py 的 `head, tail = self._split(history)`：哪段被压、哪段留原文
  - context_window.py 的 `for label, pattern in IDENTIFIER_PATTERNS:`：关键标识符是怎么被捞住的
  - retrieval.py 的 `hybrid=self.vector_weight * v + self.bm25_weight * b`：7:3 融合
  - long_term.py 的 `remember()`：策展文件和追加日志走的是两条路
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from common import trace  # noqa: E402
from common.llm import Message, count_message_tokens, estimate_tokens  # noqa: E402
from common.tools import ToolRegistry  # noqa: E402
from context_window import ContextWindow  # noqa: E402
from long_term import EPISODIC, PROCEDURAL, SEMANTIC, LongTermMemory, MemoryRecord  # noqa: E402

WORKSPACE = pathlib.Path(__file__).resolve().parent / "workspace"

SYSTEM_PROMPT = """你是差旅助手。记住用户提过的订单号、日期和偏好，不要重复问已经告诉过你的信息。"""


# ==========================================================================
# 演示 1 + 2：短期记忆与压缩
# ==========================================================================


def build_conversation() -> list[Message]:
    """造一段真实形状的多轮对话：用户诉求 + 工具结果 + 带理由的决策。"""
    return [
        {"role": "user", "content": "帮我看看订单 T20260920-0001 能不能改签，我下周要去上海。"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "query_order",
                        "arguments": '{"order_id": "T20260920-0001"}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "name": "query_order",
            "content": "订单 T20260920-0001：北京南 -> 上海虹桥，乘车日期 2026-09-20，二等座，票价 553 元，状态 已出票。",
        },
        {
            "role": "assistant",
            "content": "这个订单可以改签。因为改签规则要求新车次在开车前 24 小时以上，所以建议你今天就定下来。",
        },
        {"role": "user", "content": "好，那改到 2026-09-21 的 G102 吧。另外我一直想要靠窗的座位。"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_2",
                    "type": "function",
                    "function": {
                        "name": "change_flight",
                        "arguments": '{"order_id": "T20260920-0001", "new_train": "G102", "seat_pref": "靠窗"}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_2",
            "name": "change_flight",
            "content": "改签失败：G102 在 2026-09-21 已无靠窗余票。因为余票紧张，所以系统拒绝了本次改签。还需要确认是否接受过道座位。",
        },
        {
            "role": "assistant",
            "content": "G102 没有靠窗余票了。所以暂时没有改签。下一步我需要你确认是否接受过道座位，或者换一个车次。",
        },
        {"role": "user", "content": "那先别改，我再想想。对了，报销的话发票抬头要写什么？"},
        {
            "role": "assistant",
            "content": "报销发票抬头需要写「某某科技有限公司」，这条规矩你之前确认过。待办：等你决定车次后再执行改签。",
        },
        {"role": "user", "content": "还有，我 2026-09-22 要从上海回北京，一起看看有没有合适的车次。"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_3",
                    "type": "function",
                    "function": {
                        "name": "search_trains",
                        "arguments": '{"origin": "上海虹桥", "destination": "北京南", "date": "2026-09-22"}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_3",
            "name": "search_trains",
            "content": "2026-09-22 上海虹桥 -> 北京南：G104 二等座 553 元，14:00 发车；G106 二等座 553 元，15:30 发车。",
        },
        {
            "role": "assistant",
            "content": "因为返程也是 553 元，所以往返合计 1106 元。返程我建议订 G104，因为 14:00 发车当天到得早。",
        },
        {"role": "user", "content": "行，返程先按 G104 记着。另外提醒我一下，我的会员权益里退票是不是免手续费？"},
        {
            "role": "assistant",
            "content": "是的，你是白金卡会员，退票免手续费。待办：等你确认后一起处理去程改签和返程 G104 的预订。",
        },
    ]


def demo_1_and_2_short_term_memory() -> None:
    trace.section(
        "演示 1：短期记忆 = 上下文窗口，它会一直涨",
        "笔记第 5 节：短期记忆包含最近的对话消息、智能体回复、工具调用结果和反思内容",
    )
    conversation = build_conversation()
    # 真实的上下文窗口是 20k tokens 级别（笔记里 OpenClaw 用的是 20,000）。
    # 这里刻意缩到 700，好让压缩在一次演示里就能触发、看得见。
    window = ContextWindow(
        SYSTEM_PROMPT, max_tokens=700, keep_recent_tokens=180, keep_recent_messages=3
    )
    trace.kv("窗口上限（真实系统约 20000，这里缩小以便观察）", window.max_tokens)

    print()
    for index, message in enumerate(conversation, start=1):
        window.append(message)
        role = message.get("role")
        cost = count_message_tokens([message])
        marker = "  <-- 超过 80%，该压缩了" if window.needs_compaction() else ""
        print(
            f"  第 {index:>2} 条 ({role:<9}) +{cost:>4} token   "
            f"累计 {window.tokens():>5} / {window.max_tokens}  ({window.usage():>4.0%}){marker}"
        )

    print()
    trace.kv("窗口状态", window.stats())
    trace.note("上下文是单调增长的。它不会自己变小 —— 所以必须有 Compaction。")

    # ---- 演示 2：压缩 ----
    trace.section(
        "演示 2：Compaction —— 压成「结构化摘要 + 最近原文」",
        "笔记：摘要中会强制保留任务状态、决策理由、TODO 和关键标识符",
    )
    result = window.compact()
    assert result is not None
    print()
    trace.kv("压缩前 token", result.before_tokens)
    trace.kv("压缩后 token", result.after_tokens)
    trace.kv("省下", f"{result.saved} token（压到原来的 {result.ratio:.0%}）")
    trace.kv("被压掉的消息", f"{result.dropped} 条")
    trace.kv("保留原文的消息", f"{result.kept} 条")

    print()
    trace.rule("-")
    print("  压缩后的摘要（这就是喂给模型的那段）：")
    trace.rule("-")
    summary_text = window.messages[1]["content"]
    for line in str(summary_text).splitlines():
        print(f"    {line}")

    print()
    trace.rule("-")
    print("  四类强制保留的信息，逐项核对：")
    trace.rule("-")
    for label, values in [
        ("任务状态", [result.summary.goal] if result.summary.goal else []),
        ("决策理由", result.summary.decisions),
        ("TODO", result.summary.todos),
        ("关键标识符", result.summary.key_identifiers),
    ]:
        trace.kv(label, f"{len(values)} 条")
        for value in values[:4]:
            print(f"      - {value}")
        if len(values) > 4:
            print(f"      ...（还有 {len(values) - 4} 条）")
    trace.kv("后续用户消息", f"{result.summary.followups} 条（已折叠成计数）")
    trace.kv("工具调用统计", dict(result.summary.tool_calls))

    trace.note("盯住「关键标识符」这一栏：订单号 T20260920-0001、车次 G102、")
    trace.note("日期 2026-09-21、金额 553 元全都在。")
    trace.note("摘要如果把订单号吃掉了，后面所有工具调用都没法执行 ——")
    trace.note("这就是笔记强调「强制保留」而不是「尽量保留」的原因。")

    print()
    trace.kv("压缩后窗口状态", window.stats())
    trace.note("注意摘要本身也是一条消息、也占 token —— 压缩不是免费的，只是划算。")
    trace.note("省下的多少取决于历史堆了多长：历史越长、摘要越短，收益越大。")

    # ---- 演示 2 续：继续对话，看"锯齿形" ----
    print()
    trace.section(
        "演示 2 续：继续对话 —— 上下文是一条锯齿，不是一条直线",
        "每次涨到 80% 就压缩一次，压完继续涨",
    )
    history: list[int] = []
    survival: list[dict[str, object]] = []
    for turn in range(1, 13):
        window.append({"role": "user", "content": f"第 {turn} 轮追问：再帮我确认一次 G102 的余票情况。"})
        window.append(
            {
                "role": "assistant",
                "content": f"第 {turn} 轮查询完成：G102 靠窗余票 0 张，所以仍然无法改签。待办：等你决定是否接受过道座位。",
            }
        )
        compacted = ""
        if window.needs_compaction():
            result_n = window.compact()
            if result_n:
                compacted = f"  <-- 触发压缩：{result_n.before_tokens} -> {result_n.after_tokens}"
        history.append(window.tokens())
        print(f"  第 {turn:>2} 轮  累计 {window.tokens():>5} / {window.max_tokens}  ({window.usage():>4.0%}){compacted}")

        # 每一轮记一下四类信息还剩多少、最早那个订单号还在不在
        latest = window.compactions[-1].summary if window.compactions else None
        if latest is not None:
            survival.append(
                {
                    "轮次": turn,
                    "决策": len(latest.decisions),
                    "待办": len(latest.todos),
                    "标识符": len(latest.key_identifiers),
                    "最初的订单号还在吗": "在"
                    if any("T20260920-0001" in item for item in latest.key_identifiers)
                    else "丢了",
                }
            )

    print()
    trace.kv("token 变化序列", history)
    trace.kv("累计压缩次数", len(window.compactions))
    trace.note("没有 Compaction 的话，这条曲线会一路涨到爆窗口；")
    trace.note("有了它，上下文在「涨 -> 压 -> 涨 -> 压」之间循环，始终留在上限以内。")

    # ---- 演示 2 续二：压了 12 次之后，四类信息各自还剩什么 ----
    print()
    trace.section(
        "演示 2 续二：压缩会反复发生 —— 四类信息各自的「存活规则」不一样",
        "摘要会被插回消息列表，下一轮压缩时又经过同一个抽取函数。它们能不能活下来，取决于抽取时的角色门槛",
    )
    trace.summary_table(
        survival[::3] + [survival[-1]] if survival else [],
        ["轮次", "决策", "待办", "标识符", "最初的订单号还在吗"],
    )
    print()
    trace.note("盯住两列：")
    trace.note("  「决策」不会每压一次就整批归零（见下），也不会无限堆积 —— 它稳定在上限附近。")
    trace.note("  「最初的订单号」12 轮压缩之后仍然在。")
    trace.note("")
    trace.note("同一份摘要里，一个差点丢掉、一个永生，原因是抽取时的角色判断不同：")
    trace.note("  decisions 只认 role in {assistant, tool}，而摘要的 role 是 system ——")
    trace.note("  它下一轮走同一个函数时**认不出自己写下的决策**，于是整批消失。")
    trace.note("  key_identifiers 扫所有角色，那句「关键标识符（不可丢失）：…」被再扫一遍，")
    trace.note("  于是一路捞回来。")
    trace.note("")
    trace.note("修法是显式交接：compact() 把上一次的摘要作为种子（carry）传给 summarizer，")
    trace.note("而不是指望它能从自己的文本里被重新认出来。")
    trace.note("同时 cap() 对两类信息的取舍方向相反 —— 决策/待办留**最新**的，")
    trace.note("标识符留**最早**的。一律留最早的话，第一批决策会永生，新决策永远进不来。")

    # ---- 演示 2 续三：窗口已经超了，压缩却动不了 ----
    print()
    trace.section(
        "演示 2 续三：窗口已经 220%，压缩却压不动 —— 静默失效的那条路径",
        "笔记说「接近窗口上限时压缩」。但接近了、也说要压，不代表真的压得动",
    )
    cramped = ContextWindow(SYSTEM_PROMPT, max_tokens=1500, keep_recent_tokens=500, keep_recent_messages=4)
    for index in range(4):
        body = "再确认一次改签规则和余票情况。" * 60
        cramped.append({"role": "user", "content": f"第 {index + 1} 段。{body}"})
    head, tail = cramped._split(cramped.messages[1:])

    print()
    trace.kv("窗口上限 max_tokens", cramped.max_tokens)
    trace.kv("要保留的最近原文 keep_recent_messages", f"{cramped.keep_recent_messages} 条")
    trace.kv("当前 token", cramped.tokens())
    trace.kv("使用率", f"{cramped.usage():.2f}（早就该压了）")
    trace.kv("needs_compaction()", cramped.needs_compaction())
    print()
    trace.kv("这一刀切下去 head / tail", f"{len(head)} 条 / {len(tail)} 条")
    trace.note("看清楚这个配置有多矛盾：4 条历史一共 %d token，" % cramped.tokens())
    trace.note("平均每条约 %d token —— **光是保住 4 条原文就已经超出窗口上限了**。" % (cramped.tokens() // 4))
    trace.note("「留住最近 4 条」和「留在窗口以内」这两件事，在这个配置下没法同时成立。")

    print()
    cramped_result = cramped.compact()
    trace.kv("compact() 返回", "None（压不动）" if cramped_result is None else "压了")
    if cramped_result is not None:
        trace.kv("压掉 / 保留", f"{cramped_result.dropped} 条 / {cramped_result.kept} 条")
        trace.kv("压完仍在窗口之上", cramped_result.over_budget_after)
    trace.note("")
    trace.note("修好之前这里是 **None**：_split 的停手条件是 **and**（条数先满足，")
    trace.note("token 阈值才生效），历史条数还没到 4 条时循环一次都不 break，")
    trace.note("tail 把整段历史全收走，head 空掉，compact() 静默返回。")
    trace.note("表现是：usage() 一路涨、needs_compaction() 一直说「该压了」，")
    trace.note("而压缩一次都没发生 —— **窗口看起来有保护，其实没有。**")
    trace.note("")
    trace.note("现在 _split 保证至少留一条给 head：宁可少留一条原文，也要让这一步有进展。")
    trace.note("但「有进展」不等于「压下来了」—— 所以要如实报出 over_budget_after。")
    trace.note("压缩是尽力而为，它不是窗口的硬保护。")


# ==========================================================================
# 演示 3：长期记忆落盘
# ==========================================================================


def seed_long_term_memory(memory: LongTermMemory) -> None:
    """写入一批记忆。curated 标签的进 MEMORY.md，其余进当天日志。"""
    curated = [
        MemoryRecord("用户偏好靠窗座位，其次接受过道。", kind=SEMANTIC, tags=("curated", "偏好")),
        MemoryRecord("用户是白金卡会员，退票免手续费。", kind=SEMANTIC, tags=("curated", "会员")),
        MemoryRecord("报销发票抬头必须写「某某科技有限公司」。", kind=SEMANTIC, tags=("curated", "报销")),
        MemoryRecord("3 小时以内的行程优先订高铁，不订飞机。", kind=PROCEDURAL, tags=("curated", "规范")),
        MemoryRecord("改签规则：新车次必须在开车前 24 小时以上。", kind=PROCEDURAL, tags=("curated", "规范")),
    ]
    episodic = [
        MemoryRecord("改签订单 T20260910-0007 成功，北京南 -> 上海虹桥，G102。", kind=EPISODIC),
        MemoryRecord("查询过北京南到上海虹桥的票价，二等座 553 元。", kind=EPISODIC),
        MemoryRecord("用户投诉过一次行李托运等待时间过长。", kind=EPISODIC),
        MemoryRecord("用户问过退订政策，当时没有给出明确答复。", kind=EPISODIC),
        MemoryRecord("订单 T20260920-0001 改签失败，G102 靠窗余票售罄。", kind=EPISODIC),
        MemoryRecord("用户提到下周三要去上海出差，需要当天往返。", kind=EPISODIC),
    ]
    for record in curated + episodic:
        memory.remember(record)


def demo_3_long_term_memory() -> None:
    trace.section(
        "演示 3：长期记忆 = 工作空间里的 Markdown 文件",
        "笔记 5 节：MEMORY.md 存策展过的持久知识和偏好，memory/YYYY-MM-DD.md 按日期存追加式日志",
    )
    memory = LongTermMemory(WORKSPACE)
    memory.reset()
    seed_long_term_memory(memory)
    documents = memory.load_documents()

    print()
    trace.kv("策展文件", memory.curated_path.relative_to(WORKSPACE.parent))
    trace.kv("日志文件", memory.log_path.relative_to(WORKSPACE.parent))
    trace.kv("切出的可检索片段", f"{len(documents)} 条")
    trace.note("这两类文件写法不同：")
    trace.note("  MEMORY.md  策展的 —— 内容少、经过整理、长期有效（偏好/规范/教训）")
    trace.note("  memory/*.md 追加的 —— 只写不改、按时间累积（今天发生了什么）")

    print()
    trace.rule("-")
    print(f"  {memory.curated_path.name} 的内容：")
    trace.rule("-")
    for line in memory.curated_path.read_text(encoding="utf-8").strip().splitlines():
        print(f"    {line}")

    print()
    trace.rule("-")
    print(f"  memory/{memory.log_path.name} 的内容：")
    trace.rule("-")
    for line in memory.log_path.read_text(encoding="utf-8").strip().splitlines():
        print(f"    {line}")

    trace.note(f"文件真实落在 {WORKSPACE}，可以直接打开看。")


# ==========================================================================
# 演示 4：混合检索 7:3
# ==========================================================================

QUERIES = [
    ("改期怎么操作", "换个说法问同一件事：记忆里写的是「改签」，一个词都对不上"),
    ("T20260910-0007", "罕见专有串，看 BM25 的高 IDF 优势"),
    ("报销发票抬头怎么写", "两者都能中，看融合后的排序"),
]


def demo_4_hybrid_retrieval() -> None:
    trace.section(
        "演示 4：混合检索 —— 向量 0.7 + BM25 0.3",
        "笔记：同时使用向量语义搜索和 BM25 关键词搜索，按 7:3 权重做混合排序",
    )
    memory = LongTermMemory(WORKSPACE)
    memory.load_documents()

    for query, note in QUERIES:
        print()
        trace.section(f"查询：{query}", note)
        bm25 = memory.retriever.bm25.search(query)
        vector = memory.retriever.vector.search(query)
        hits = memory.search(query, top_k=3)

        trace.kv("BM25 命中文档数", len(bm25))
        trace.kv("向量命中文档数", len(vector))
        print()
        trace.summary_table(
            [
                {
                    "排名": index,
                    "融合分": f"{hit.hybrid:.3f}",
                    "向量": f"{hit.vector:.3f}",
                    "BM25": f"{hit.bm25:.3f}",
                    "向量原始分": f"{hit.vector_raw:.3f}",
                    "BM25原始分": f"{hit.bm25_raw:.2f}",
                    "内容": hit.text[:30],
                }
                for index, hit in enumerate(hits, start=1)
            ],
            ["排名", "融合分", "向量", "BM25", "向量原始分", "BM25原始分", "内容"],
        )

    print()
    trace.note("三个查询连起来看，两种检索的性格差别就出来了：")
    trace.note("")
    trace.note("  查询 1：BM25 完全零命中 —— 用户换个说法问同一件事，关键词检索就瞎了。")
    trace.note("          是向量靠两句话共享的一个「改」字，把三条改签相关的记忆捞了回来。")
    trace.note("  查询 2：反过来。订单号这种罕见串，BM25 一击命中且只命中 1 条，非常干净；")
    trace.note("          向量的命中靠的是字符重叠而不是理解，精度不如它。")
    trace.note("  查询 3：两者都能命中正确文档。向量额外给出的几条分数掉到了 0.01 量级，")
    trace.note("          基本是噪声 —— 好在这点分数被 0.3 的权重压住，翻不了盘。")
    trace.note("")
    trace.note("一句话：BM25 少而准，向量广而糙。融合之后精确项由 BM25 顶住，")
    trace.note("漏掉的由向量兜底 —— 这就是 7:3 混合排序的意义。")
    print()
    trace.note("现在对照两列数字看「归一化」干了什么（以查询 3 的第一名为例）：")
    trace.note("  BM25 原始分 11.01，向量原始分 0.604 —— 差了将近 20 倍。")
    trace.note("  但归一化之后，两路的第一名都是 **1.000**。")
    trace.note("  这就是「除以本路最高分」的代价：**绝对量级被抹平，只剩相对比例**。")
    trace.note("  所以两路不能共用一个分母 —— BM25 无上界、余弦在 [0,1]，")
    trace.note("  共用一个分母会让 BM25 单方面压倒那 0.3 的权重。")
    trace.note("  留这两列原始分，就是为了让被抹掉的东西还能被看见。")
    print()
    trace.note("还要注意查询 1 那一行的融合分：**1.000**，不是 0.700。")
    trace.note("  权重只在「这一路真的有结果」之间分配（加权平均），不是死板的 0.7/0.3。")
    trace.note("  否则某一路零命中时，它那 0.3 不是「贡献 0」而是**从公式里整体消失**：")
    trace.note("  另一路兜回来的文档天花板只剩 0.7，而且同一个文档的分数会随")
    trace.note("  「另一路这次有没有命中」跳变，跨查询根本没法比。")
    trace.note("  那等于把「兜底」悄悄变成了「降级」——折扣多大还随查询变。")
    trace.note("")
    trace.note("诚实说明：这里的「向量」是字符特征替身，不是真 embedding。")
    trace.note("查询 1 它只靠共享一个「改」字捞到结果；换成真 embedding，")
    trace.note("它还能靠语义直接对齐「改期」和「改签」，召回会明显更好。")


# ==========================================================================
# 演示 5：按需检索 vs 全量注入
# ==========================================================================


def demo_5_retrieve_on_demand() -> None:
    trace.section(
        "演示 5：memory_search 按需检索，而不是把记忆全塞进上下文",
        "笔记：Agent 需要回忆历史时，通过 memory_search 工具按需检索相关片段再拉进上下文",
    )
    memory = LongTermMemory(WORKSPACE)
    documents = memory.load_documents()

    registry = ToolRegistry("p04")
    registry.register(memory.as_tool())

    full_text = memory.all_text()
    full_tokens = estimate_tokens(full_text)

    question = "帮我改签到 2026-09-21，靠窗。"
    print()
    trace.kv("用户问题", question)

    # 路线 A：把全部长期记忆塞进上下文
    print()
    trace.rule("-")
    print("  路线 A：把全部长期记忆注入上下文")
    trace.rule("-")
    trace.kv("注入内容", f"{len(documents)} 条记忆，全部")
    trace.kv("占用 token", full_tokens)
    trace.note("问题在于：这 {0} 条里，这次任务真正用得上的可能只有 2 条。".format(len(documents)))
    trace.note("其余的是纯噪声，既花钱又干扰模型注意力。")

    # 路线 B：按需检索
    print()
    trace.rule("-")
    print("  路线 B：模型调用 memory_search 按需检索")
    trace.rule("-")
    result = registry.call("memory_search", {"query": question, "top_k": 3})
    if not result.ok:
        trace.kv("检索失败", result.error or result.protocol_error)
        return

    hits = result.structured.get("results", [])
    trace.kv("检索到", f"{len(hits)} 条")
    for hit in hits:
        print(f"      [{hit['source']}] {hit['text']}")
    retrieved_text = "".join(hit["text"] for hit in hits)
    retrieved_tokens = estimate_tokens(retrieved_text)
    trace.kv("占用 token", retrieved_tokens)

    print()
    trace.summary_table(
        [
            {"路线": "A 全量注入", "注入条数": len(documents), "占用token": full_tokens,
             "相对开销": "100%"},
            {"路线": "B 按需检索", "注入条数": len(hits), "占用token": retrieved_tokens,
             "相对开销": f"{retrieved_tokens / full_tokens:.0%}"},
        ],
        ["路线", "注入条数", "占用token", "相对开销"],
    )
    trace.note("记忆越多，这个差距越大 —— 这就是 RAG 式长期记忆要「按需检索」的原因。")
    trace.note("注意：按需检索也意味着模型得先意识到「我该去查记忆」，")
    trace.note("所以 memory_search 工具的 description 写得够不够清楚，直接决定它会不会被调用。")


def main() -> None:
    trace.section(
        "原型 04：Agent 的记忆功能",
        "笔记第 5 章（+ 11.3 OpenClaw / 12.3 Hermes 的记忆系统细节）",
    )
    trace.note("短期记忆 = 上下文窗口，满了就 Compaction 压成「结构化摘要 + 最近原文」")
    trace.note("长期记忆 = 环境外的存储，需要时用 memory_search 按需检索")

    demo_1_and_2_short_term_memory()
    demo_3_long_term_memory()
    demo_4_hybrid_retrieval()
    demo_5_retrieve_on_demand()

    trace.section("记忆类型对照（笔记 5.4.1）")
    trace.summary_table(
        [
            {"类型": "情景记忆", "存什么": "什么时候发生了什么", "存在哪": "memory/YYYY-MM-DD.md（追加）"},
            {"类型": "语义记忆", "存什么": "事实和知识", "存在哪": "MEMORY.md（策展）"},
            {"类型": "程序记忆", "存什么": "怎么做一件事、有什么规矩", "存在哪": "MEMORY.md（策展）"},
            {"类型": "短期记忆", "存什么": "当前这轮对话的上下文", "存在哪": "上下文窗口（会压缩）"},
        ],
        ["类型", "存什么", "存在哪"],
    )


if __name__ == "__main__":
    main()
