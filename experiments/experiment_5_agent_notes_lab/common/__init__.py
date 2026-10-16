"""experiment_5 各原型的公共零件。

只有三样东西：模型抽象（llm）、工具抽象（tools）、打印工具（trace）。
刻意保持很小——每个原型都要能单独读懂，公共层一旦变大就没人看了。

对应笔记：07-Agent开发框架与工程实践.md
"""
from __future__ import annotations

import sys

# PyCharm 的 Run 控制台一般已经是 UTF-8，但用管道重定向输出时 Windows 会退回
# cp936，中文直接抛 UnicodeEncodeError。这里统一一次，后面所有原型都不用再管。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, ValueError):  # pragma: no cover - 非标准流
        pass
