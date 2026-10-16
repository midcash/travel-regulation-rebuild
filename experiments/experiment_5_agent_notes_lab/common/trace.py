"""打印工具：让 PyCharm 控制台里的运行轨迹一眼能看懂。

调试的时候最怕两件事：输出一大坨看不清、关键中间态没打出来。
这里的函数都是给"人看"的，不是给日志系统看的。
"""
from __future__ import annotations

import json
import unicodedata
from typing import Any

WIDTH = 78


def rule(char: str = "-") -> None:
    print(char * WIDTH)


def section(title: str, subtitle: str = "") -> None:
    print()
    rule("=")
    print(f"  {title}")
    if subtitle:
        print(f"  {subtitle}")
    rule("=")


def step(index: int, title: str, note: str = "") -> None:
    print()
    print(f"[第 {index} 步] {title}")
    if note:
        print(f"          {note}")
    rule("-")


def kv(key: str, value: Any, indent: int = 2) -> None:
    print(f"{' ' * indent}{key}: {_fmt(value)}")


def bullet(text: str, indent: int = 2, marker: str = "-") -> None:
    print(f"{' ' * indent}{marker} {text}")


def note(text: str) -> None:
    print(f"  > {text}")


def _fmt(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def show_tool_calls(tool_calls: list[Any], indent: int = 2) -> None:
    pad = " " * indent
    for call in tool_calls:
        args = json.dumps(call.arguments, ensure_ascii=False, default=str)
        print(f"{pad}-> 调用工具 {call.name}({args})")


def show_messages(messages: list[dict[str, Any]], limit_content: int = 220) -> None:
    """把完整的对话历史打出来——ReAct 循环的产物就是它。"""
    print()
    rule("-")
    print(f"  完整对话历史（共 {len(messages)} 条消息）")
    rule("-")
    for i, msg in enumerate(messages):
        role = msg.get("role", "?")
        content = str(msg.get("content") or "")
        if len(content) > limit_content:
            content = content[:limit_content] + f"...（共 {len(content)} 字）"
        content = content.replace("\n", "\n" + " " * 14)
        print(f"  [{i:02d}] {role:<9} {content}")
        for call in msg.get("tool_calls") or []:
            fn = call.get("function", {})
            print(f"       {'':<9} -> {fn.get('name')}({fn.get('arguments')})")
        if msg.get("tool_call_id"):
            print(f"       {'':<9} (回复 {msg['tool_call_id']})")


def show_tools(schemas: list[dict[str, Any]]) -> None:
    print()
    print(f"  可用工具（{len(schemas)} 个）：")
    for schema in schemas:
        fn = schema.get("function", {})
        required = fn.get("parameters", {}).get("required", [])
        print(f"    - {fn.get('name')}  必填={required}")


def display_width(text: str) -> int:
    """中文在终端里占两格，用 len() 对不齐表格，所以自己数一遍。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - display_width(text))


def summary_table(rows: list[dict[str, Any]], columns: list[str]) -> None:
    """打印一张对齐的小表，用于对比基线 / 候选这类结果。"""
    if not rows:
        print("  (空)")
        return
    widths = {
        col: max(display_width(col), *(display_width(_fmt(r.get(col, ""))) for r in rows))
        for col in columns
    }
    header = "  ".join(_pad(col, widths[col]) for col in columns)
    print("  " + header)
    print("  " + "-" * display_width(header))
    for row in rows:
        print("  " + "  ".join(_pad(_fmt(row.get(col, "")), widths[col]) for col in columns))
