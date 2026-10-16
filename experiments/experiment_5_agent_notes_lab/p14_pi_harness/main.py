"""原型 14：Pi 的小内核、会话树和压缩策略。"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from common import trace  # noqa: E402
from session_tree import AppendOnlySession, PiAgent  # noqa: E402


ROOT = pathlib.Path(__file__).resolve().parent


def demo_tree() -> None:
    session = AppendOnlySession(ROOT / "workspace" / "demo-session.jsonl")
    agent = PiAgent(session)
    root = agent.user("修复测试失败")
    agent.assistant("先读取测试输出")
    branch = agent.tool("bash", "发现 import 错误")
    abandoned = agent.assistant("尝试方案 A")
    session.move_leaf(branch.id)
    chosen = agent.assistant("改用方案 B")
    trace.section("演示 1：回退只挪指针，不删除历史")
    trace.kv("JSONL 中所有条目", list(session.entries))
    trace.kv("当前 leaf", session.leaf_id)
    trace.kv("当前上下文链", session.model_context())
    trace.kv("被放弃分支仍存在", abandoned.id in session.entries)
    trace.note("文件里 A/B 两个分支都在；模型只沿当前 leaf 的 parentId 链看到一条。")
    trace.kv("选中的新分支", chosen.id)


def demo_compaction() -> None:
    session = AppendOnlySession(ROOT / "workspace" / "demo-compaction.jsonl")
    agent = PiAgent(session)
    first = agent.user("做一个旅行报价")
    agent.assistant("已确认出发地和目的地")
    keep = agent.tool("search", "找到三趟候选航班")
    agent.assistant("继续比较价格")
    agent.tool("search", "工具返回价格与行李规则")
    session.compact("## Goal\n生成旅行报价\n## Progress\n已找到候选航班", keep.id)
    agent.user("优先选择含行李的方案")
    trace.section("演示 2：压缩是追加节点，不改旧消息")
    trace.kv("存储条目数", len(session.entries))
    trace.kv("模型看到的上下文", session.model_context())
    trace.kv("最早的原始消息仍在", first.id in session.entries)
    trace.note("回退是换起点，压缩是换中段；二者都不需要删除 JSONL 历史。")


def main() -> None:
    trace.section("原型 14：Pi-Agent Harness", "第 14 章：小内核 + 扩展、AgentLoop、JSONL 会话树、Compaction")
    trace.note("Pi 的学习价值不在功能多，而在核心边界清楚：loop、session、压缩、provider 都写在明面上。")
    demo_tree()
    demo_compaction()
    trace.section("重要边界")
    trace.kv("会话回退", "只改变对话 leaf")
    trace.kv("代码回退", "内核不负责；需要 git-checkpoint 扩展")


if __name__ == "__main__":
    main()
