"""短期记忆：上下文窗口 + Compaction（压缩）。

对应笔记第 5 节对 OpenClaw 的描述：

    当上下文 token 接近模型窗口上限时，系统会把最近约 20,000 tokens 的消息
    原样保留，更早的消息则用 LLM 分块生成结构化摘要来替代，摘要中会强制保留
    任务状态、决策理由、TODO 和关键标识符等信息，压缩后上下文变为
    「摘要 + 最近原文」的结构。

这个文件就是把这段话写成代码。三个要点：

  1. 窗口有上限，所以必须压。压的时机是"接近上限"，不是"已经爆了"。
  2. 压的方法不是简单丢弃，也不是让模型随便总结 —— 而是**分块 + 结构化**。
  3. 摘要里必须**强制保留**四类信息：任务状态、决策理由、TODO、关键标识符。
     这四类丢了，Agent 后面就会重复问同样的问题、重复踩同样的坑。

这里的"生成摘要"用规则实现（正则 + 关键词），不调模型。原因和别处一样：
规则实现是确定性的，你能在调试器里看清到底是哪条消息、哪句话被保留了下来。
真实系统里这一步交给 LLM，但**保留什么**这件事仍然由这段规则定义。
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from common.llm import Message, count_message_tokens

# --------------------------------------------------------------------------
# 强制保留的"关键标识符"
#
# 笔记特别点出这一类：它们是最容易被摘要吃掉、又最不该被吃掉的东西。
# 摘要写成"用户要改签一张票"就完了，但订单号 T20260920-0001 一旦丢了，
# 后面所有工具调用都没法执行。
# --------------------------------------------------------------------------

IDENTIFIER_PATTERNS: list[tuple[str, str]] = [
    ("订单号", r"T\d{8}-\d{4}"),
    ("航班号", r"\b[A-Z]{2}\d{3,4}\b"),
    ("车次", r"\b[GDC]\d{1,4}\b"),
    ("日期", r"\d{4}-\d{2}-\d{2}"),
    ("金额", r"\d+(?:\.\d+)?\s*元"),
    ("车站", r"(?:北京南|上海虹桥|广州南|深圳北|杭州东)"),
]

DECISION_MARKERS = ("因为", "所以", "因此", "于是", "决定", "改用", "改为", "降级", "放弃", "选择")
TODO_MARKERS = ("todo", "待办", "还需要", "还没", "下一步", "待确认", "记得", "需要补充")


@dataclass
class StructuredSummary:
    """压缩后的摘要。四类信息分开存，是为了让"强制保留"这件事看得见。

    摘要的第一原则是**必须比原文短**。四类信息各自有上限：
    超过上限就折叠成"N 条已省略"，而不是无限增长 ——
    否则压缩会越压越大（这是一个很容易踩的坑，见 main.py 演示 2 的对比）。
    """

    goal: str = ""
    followups: int = 0
    tool_calls: Counter[str] = field(default_factory=Counter)
    decisions: list[str] = field(default_factory=list)
    todos: list[str] = field(default_factory=list)
    key_identifiers: list[str] = field(default_factory=list)
    omitted: Counter[str] = field(default_factory=Counter)

    MAX_DECISIONS = 4
    MAX_TODOS = 4
    MAX_IDENTIFIERS = 10

    def cap(self) -> None:
        """把每一类截到上限，多出来的折叠计数。

        两类信息的取舍方向**正好相反**，这不是随手写的：

            decisions / todos —— 留**最新**的。它们描述"现在该做什么"，
                                 陈年决策留在摘要里，只会挤掉新决策的位置。
            key_identifiers   —— 留**最早**的。订单号、车次这些是任务对象本身，
                                 最早出现的那批往往就是这次任务的根本，
                                 而且它们一旦丢了就再也捞不回来。

        以前一律 `values[:limit]`（都留最早）。在摘要还是"一次性产物"的时候
        这没什么问题；但摘要会被重新汇入下一次压缩（见 summarize 的 carry），
        一律留最早就会变成「第一批决策永生，后面的永远进不来」。
        """
        for name, limit, keep_newest in (
            ("decisions", self.MAX_DECISIONS, True),
            ("todos", self.MAX_TODOS, True),
            ("key_identifiers", self.MAX_IDENTIFIERS, False),
        ):
            values: list[str] = getattr(self, name)
            if len(values) > limit:
                self.omitted[name] = len(values) - limit
                setattr(self, name, values[-limit:] if keep_newest else values[:limit])

    def render(self) -> str:
        lines = ["【历史对话摘要】（原始消息已压缩，以下为保留的关键信息）"]

        state = []
        if self.goal:
            state.append(f"目标：{self.goal}")
        if self.followups:
            state.append(f"后续用户消息 {self.followups} 条（内容已压缩）")
        if self.tool_calls:
            calls = "、".join(f"{name}×{count}" for name, count in self.tool_calls.items())
            state.append(f"已调用工具：{calls}")
        if state:
            lines.append("· 任务状态：")
            lines += [f"    - {item}" for item in state]

        if self.decisions:
            lines.append("· 已做的决策与理由：")
            lines += [f"    - {item}" for item in self.decisions]

        if self.todos:
            lines.append("· 待办：")
            lines += [f"    - {item}" for item in self.todos]

        if self.key_identifiers:
            lines.append("· 关键标识符（不可丢失）：")
            lines.append("    - " + "；".join(self.key_identifiers))

        for name, label in (
            ("decisions", "决策"),
            ("todos", "待办"),
            ("key_identifiers", "标识符"),
        ):
            if self.omitted.get(name):
                lines.append(f"· （另有 {self.omitted[name]} 条{label}因超上限被省略）")

        if len(lines) == 1:
            lines.append("（这段历史没有需要保留的结构化信息）")
        return "\n".join(lines)


class Summarizer:
    """把一批消息压成 StructuredSummary。

    "分块"对应笔记里的"用 LLM 分块生成结构化摘要"：太长的历史一次总结容易丢细节，
    切成小块分别总结，再把结果合并去重。
    """

    def __init__(self, chunk_size: int = 6):
        self.chunk_size = chunk_size

    def summarize(
        self, messages: list[Message], carry: StructuredSummary | None = None
    ) -> StructuredSummary:
        """把一批消息压成 StructuredSummary。

        `carry` 是**上一次压缩留下的摘要**，必须显式传进来。

        为什么不能省：摘要会被插回消息列表（role=system），
        下一轮压缩时它作为历史的开头再次经过这个函数。
        而 `decisions` 的抽取有角色门槛（只认 assistant / tool），
        摘要的 role 是 system —— 于是第 1 次压缩记下的决策，
        在第 2 次压缩后就整批消失了。

        同一份摘要里，`key_identifiers` 却能活下来：它扫所有角色，
        于是那句「关键标识符（不可丢失）：…」被再扫一遍、再捞回来。

        **一个丢了、一个活着，原因只是两行代码的角色判断不同。**
        所以这里改成显式交接：上一次摘要里还没过期的信息，
        作为种子进入这一次的摘要，而不是指望它能从文本里被重新认出来。
        """
        summary = StructuredSummary()
        if carry is not None:
            summary.goal = carry.goal
            summary.followups = carry.followups
            summary.tool_calls = Counter(carry.tool_calls)
            summary.decisions = list(carry.decisions)
            summary.todos = list(carry.todos)
            summary.key_identifiers = list(carry.key_identifiers)

        chunks = [
            messages[i : i + self.chunk_size]
            for i in range(0, len(messages), self.chunk_size)
        ]
        for chunk in chunks:
            self._summarize_chunk(chunk, summary)
        self._dedupe(summary)
        summary.cap()
        return summary

    # -- 内部 -------------------------------------------------------------

    def _summarize_chunk(self, chunk: list[Message], summary: StructuredSummary) -> None:
        for msg in chunk:
            role = msg.get("role", "")
            text = str(msg.get("content") or "")

            # 任务状态：目标只留第一条（用户最初想干什么），
            # 后面的用户消息折叠成计数 —— 逐条抄下来会让摘要比原文还长。
            if role == "user" and text:
                if not summary.goal:
                    summary.goal = _clip(text, 50)
                else:
                    summary.followups += 1
            for call in msg.get("tool_calls") or []:
                fn = call.get("function", {})
                summary.tool_calls[str(fn.get("name"))] += 1

            # 决策与待办。注意分类优先级：一句话同时命中两类时归到"待办"，
            # 因为待办是会丢东西的那一类（"待办：等你决定车次后再执行改签"
            # 同时含"决定"和"待办"，归到决策就等于把要做的事降级成背景信息了）。
            for sentence in _sentences(text):
                lowered = sentence.lower()
                if any(marker in lowered for marker in TODO_MARKERS):
                    summary.todos.append(_clip(sentence, 50))
                elif role in {"assistant", "tool"} and any(
                    marker in sentence for marker in DECISION_MARKERS
                ):
                    summary.decisions.append(_clip(sentence, 50))

            # 关键标识符：从所有角色的文本里扫，因为订单号往往只出现在工具结果里
            for label, pattern in IDENTIFIER_PATTERNS:
                for value in re.findall(pattern, text):
                    summary.key_identifiers.append(f"{label} {value}")

    @staticmethod
    def _dedupe(summary: StructuredSummary) -> None:
        """分块总结会产生重复，合并时去重并保序。"""
        for name in ("decisions", "todos", "key_identifiers"):
            values: list[str] = getattr(summary, name)
            seen: set[str] = set()
            unique: list[str] = []
            for value in values:
                if value not in seen:
                    seen.add(value)
                    unique.append(value)
            setattr(summary, name, unique)


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"[。！？\n]", text) if s.strip()]


def _clip(text: str, limit: int = 60) -> str:
    text = text.replace("\n", " ").strip()
    return text if len(text) <= limit else text[:limit] + "…"


@dataclass
class CompactionResult:
    before_tokens: int
    after_tokens: int
    summary: StructuredSummary
    dropped: int
    kept: int
    # 压完仍在窗口之上：说明"要保留的最近原文"自己就装不下。
    # 压缩是尽力而为，不是窗口的硬保护 —— 那就得把"这次没压下来"如实报出来，
    # 而不是让调用方以为压过就一定安全了。
    over_budget_after: bool = False

    @property
    def saved(self) -> int:
        return self.before_tokens - self.after_tokens

    @property
    def ratio(self) -> float:
        if self.before_tokens == 0:
            return 0.0
        return self.after_tokens / self.before_tokens


class ContextWindow:
    """短期记忆本体：一个装不下就会自动压缩的消息列表。"""

    def __init__(
        self,
        system_prompt: str,
        max_tokens: int = 1500,
        keep_recent_tokens: int = 500,
        keep_recent_messages: int = 4,
        summarizer: Summarizer | None = None,
    ):
        # 真实的窗口是 20k tokens 级别（笔记里的 OpenClaw 用 20,000）。
        # 这里刻意缩小到几百，好让压缩在几轮对话内就能触发、看得见。
        self.max_tokens = max_tokens
        self.keep_recent_tokens = keep_recent_tokens
        self.keep_recent_messages = keep_recent_messages
        self.summarizer = summarizer or Summarizer()
        self.messages: list[Message] = [{"role": "system", "content": system_prompt}]
        self.compactions: list[CompactionResult] = []
        # 上一次压缩的产物。下一次压缩要把它当种子接过去 ——
        # 否则摘要里有些东西（比如「决策」）会在第二次压缩时整批蒸发。
        self._last_summary: StructuredSummary | None = None

    # -- 基本操作 ---------------------------------------------------------

    def append(self, message: Message) -> None:
        self.messages.append(message)

    def tokens(self) -> int:
        return count_message_tokens(self.messages)

    def usage(self) -> float:
        return self.tokens() / self.max_tokens

    def needs_compaction(self) -> bool:
        """笔记说的是"接近模型窗口上限时"触发，不是爆了才触发。"""
        return self.usage() >= 0.8

    # -- 压缩 -------------------------------------------------------------

    def compact(self) -> CompactionResult | None:
        """把较早的消息换成结构化摘要，保留最近一段原文。"""
        if len(self.messages) <= 1:
            return None

        before = self.tokens()
        system = self.messages[0]
        history = self.messages[1:]

        head, tail = self._split(history)
        if not head:
            return None  # 只剩一条历史消息 —— 确实没得压

        # 把上一次摘要当种子接过来。摘要本身也在 head 里（它是 messages[1]），
        # 会被再扫一遍 —— 那没关系：标识符和 TODO 会被去重捞回，
        # 而「决策」那一路因为角色门槛捞不回来，正是靠 carry 才活下来的。
        summary = self.summarizer.summarize(head, carry=self._last_summary)
        rebuilt: list[Message] = [system, {"role": "system", "content": summary.render()}]
        rebuilt.extend(tail)
        self.messages = rebuilt
        self._last_summary = summary

        result = CompactionResult(
            before_tokens=before,
            after_tokens=self.tokens(),
            summary=summary,
            dropped=len(head),
            kept=len(tail),
            over_budget_after=self.usage() >= 1.0,
        )
        self.compactions.append(result)
        return result

    def _split(self, history: list[Message]) -> tuple[list[Message], list[Message]]:
        """从后往前收，直到攒够 keep_recent_tokens。返回 (要压缩的, 要保留的)。"""
        tail: list[Message] = []
        used = 0
        for message in reversed(history):
            cost = count_message_tokens([message])
            if len(tail) >= self.keep_recent_messages and used + cost > self.keep_recent_tokens:
                break
            tail.append(message)
            used += cost
        tail.reverse()

        # ★ 保留段不能把历史吃光。
        #
        # 上面那个条件是 **and**：条数先够了，token 阈值才开始生效。
        # 于是历史比 keep_recent_messages 还短的时候，循环一次都不会 break，
        # tail 把整段历史全收走，head 就空了 —— compact() 静默返回 None。
        #
        # 表现出来是：usage() 一路涨到 220%，needs_compaction() 一直说"该压了"，
        # 而压缩一次都没真的发生。窗口看起来有保护，其实没有。
        #
        # 压缩确实是"尽力而为"，但**尽力**至少得意味着每一步都有进展：
        # 宁可少留一条原文，也不能让这一步变成空转。
        if tail and len(tail) == len(history):
            tail = tail[1:]  # 最老的那条让给 head（tail 已经是正序，[0] 最老）

        head = history[: len(history) - len(tail)]
        return head, tail

    # -- 给 Agent 用的视图 -------------------------------------------------

    def view(self) -> list[Message]:
        """真正发给模型的消息。"""
        return list(self.messages)

    def stats(self) -> dict[str, Any]:
        return {
            "消息条数": len(self.messages),
            "上下文 token": self.tokens(),
            "窗口上限": self.max_tokens,
            "使用率": f"{self.usage():.0%}",
            "已压缩次数": len(self.compactions),
            "压完仍超窗口": sum(1 for item in self.compactions if item.over_budget_after),
        }
