"""买票工具的四个 schema 版本，用来做 A/B 对照。

笔记 2.2 给的六条建议里，前四条都是关于 schema 本身的：
    1. 重构函数设计，参数越少越好
    2. 拆分函数，单一职责
    3. 优化 JSON Schema：类型、必填、默认值、描述
    4. 优化命名，让名字有描述性
这个文件把"没做"和"做了"两个版本并排放着，main.py 会拿同一个用户意图分别去调，
差别在输出里一目了然。

笔记 4.4 那句话是这一节的总纲：schema 本身是给 LLM 看的说明书。
"""
from __future__ import annotations

from typing import Any

from common.tools import ToolFailure

# 业务侧的真实数据：服务端才知道的合法值
KNOWN_STATIONS = {"北京南", "上海虹桥", "广州南", "深圳北", "杭州东"}
KNOWN_SEAT_CLASSES = ["二等座", "一等座", "商务座"]
KNOWN_TICKET_TYPES = ["成人票", "儿童票", "学生票"]
MAX_PASSENGERS = 5


def _check_station(value: str, field: str) -> str:
    if value not in KNOWN_STATIONS:
        # 笔记 4.4：错误信息要告诉 LLM 下一步该怎么办，而不是丢一堆堆栈。
        # 这里连"合法值有哪些"一起告诉它，它才有机会自己修。
        raise ToolFailure(
            "STATION_NOT_FOUND",
            f"没有名为 {value!r} 的车站，可用车站：{sorted(KNOWN_STATIONS)}",
            retryable=False,
            failed_fields=[field],
        )
    return value


def book_train_ticket(
    origin: str,
    destination: str,
    travel_date: str,
    passengers: int = 1,
    seat_class: str = "二等座",
    note: str = "",
) -> dict[str, Any]:
    """服务端实现。schema 校验过了不代表业务校验也能过——两层校验都要有。"""
    _check_station(origin, "origin")
    _check_station(destination, "destination")
    if origin == destination:
        raise ToolFailure(
            "INVALID_ROUTE", "出发站和到达站不能相同", retryable=False, failed_fields=["destination"]
        )
    if len(travel_date) != 10 or travel_date[4] != "-" or travel_date[7] != "-":
        raise ToolFailure(
            "INVALID_DATE_FORMAT",
            f"日期 {travel_date!r} 不是 YYYY-MM-DD 格式，例如 2026-09-20",
            retryable=False,
            failed_fields=["travel_date"],
        )
    if seat_class not in KNOWN_SEAT_CLASSES:
        # 没有 enum 收敛的后果：schema 层拦不住，只能业务层拦
        raise ToolFailure(
            "INVALID_SEAT_CLASS",
            f"没有 {seat_class!r} 这个席别，可用席别：{KNOWN_SEAT_CLASSES}",
            retryable=False,
            failed_fields=["seat_class"],
        )
    if not 1 <= passengers <= MAX_PASSENGERS:
        raise ToolFailure(
            "INVALID_PASSENGER_COUNT",
            f"一次最多买 {MAX_PASSENGERS} 张，收到 {passengers}",
            retryable=False,
            failed_fields=["passengers"],
        )
    return {
        "order_id": "T20260920-0001",
        "origin": origin,
        "destination": destination,
        "travel_date": travel_date,
        "passengers": passengers,
        "seat_class": seat_class,
        "total_price": 553 * passengers,
        "note": note or "（无）",
    }


def book_train_ticket_no_enum(
    origin: str,
    destination: str,
    travel_date: str,
    ticket_type: str,
    passengers: int = 1,
    seat_class: str = "二等座",
) -> dict[str, Any]:
    """v2 的服务端实现：票种字段没有用 enum 收敛，只能业务层兜。"""
    if ticket_type not in KNOWN_TICKET_TYPES:
        raise ToolFailure(
            "INVALID_TICKET_TYPE",
            f"没有 {ticket_type!r} 这个票种，可用票种：{KNOWN_TICKET_TYPES}",
            retryable=False,
            failed_fields=["ticket_type"],
        )
    booked = book_train_ticket(origin, destination, travel_date, passengers, seat_class)
    booked["ticket_type"] = ticket_type
    return booked


def book_train_ticket_v0(
    a: str = "",
    b: str = "",
    t: str = "",
    n: int = 1,
    c: str = "二等座",
    r: str = "",
    p: str = "",
) -> dict[str, Any]:
    """v0 的服务端实现：参数名和 schema 一样含糊。

    注意报错时用的是 schema 里的字段名（a/b/t），不是内部实现的名字——
    服务端必须用调用方看得懂的名字回话，否则结构化错误也帮不上模型。
    但即便这样，模型仍然猜不出 a 是出发站：**名字没描述性，错误信息也救不了**。
    """
    if a not in KNOWN_STATIONS:
        raise ToolFailure(
            "STATION_NOT_FOUND",
            f"没有名为 {a!r} 的车站，可用车站：{sorted(KNOWN_STATIONS)}。参数 a 表示出发站。",
            retryable=False,
            failed_fields=["a"],
        )
    if b not in KNOWN_STATIONS:
        raise ToolFailure(
            "STATION_NOT_FOUND",
            f"没有名为 {b!r} 的车站，可用车站：{sorted(KNOWN_STATIONS)}。参数 b 表示到达站。",
            retryable=False,
            failed_fields=["b"],
        )
    if t not in {"2026-09-20"}:
        raise ToolFailure("INVALID_DATE", f"日期 {t!r} 不合法。参数 t 表示乘车日期。", False, ["t"])
    if c not in KNOWN_SEAT_CLASSES:
        raise ToolFailure(
            "INVALID_SEAT_CLASS", f"没有 {c!r} 这个席别。参数 c 表示席别。", False, ["c"]
        )
    return book_train_ticket(origin=a, destination=b, travel_date=t, passengers=n, seat_class=c, note=r)


# --------------------------------------------------------------------------
# 版本 v0：没做那四条建议
# --------------------------------------------------------------------------
SCHEMA_V0: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "book_train_ticket_v0",
        "description": "买票",  # 第 5 条：描述太短，模型不知道什么时候该用它
        "parameters": {
            "type": "object",
            "properties": {
                # 第 4 条：命名毫无描述性，模型只能瞎猜
                "a": {"type": "string"},
                "b": {"type": "string"},
                "t": {"type": "string"},
                # 第 3 条：没有 description、没有 default、没有 minimum/maximum
                "n": {"type": "integer"},
                "c": {"type": "string"},
                "r": {"type": "string"},
                "p": {"type": "string"},
            },
            # 第 1、2 条：7 个必填，远超"0 个最好、1 个次之、2 个再次"的实践经验
            "required": ["a", "b", "t", "n", "c", "r", "p"],
        },
    },
}

# --------------------------------------------------------------------------
# 版本 v1：四条建议都做了
# --------------------------------------------------------------------------
SCHEMA_V1: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "book_train_ticket",
        "description": (
            "购买火车票。用户明确说了出发站、到达站和乘车日期时才调用；"
            "缺任何一个都不要猜，先向用户确认。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "origin": {"type": "string", "description": "出发站，例如 北京南"},
                "destination": {"type": "string", "description": "到达站，例如 上海虹桥"},
                "travel_date": {"type": "string", "description": "乘车日期，格式 YYYY-MM-DD"},
                "passengers": {
                    "type": "integer",
                    "description": "购票人数",
                    "default": 1,  # 能给默认值就给：模型不传也能过
                    "minimum": 1,
                    "maximum": MAX_PASSENGERS,  # 约束内联，模型和 Server 都能校验
                },
                "seat_class": {
                    "type": "string",
                    "description": "席别",
                    "enum": KNOWN_SEAT_CLASSES,  # 用枚举收敛，不让模型自由发挥
                    "default": "二等座",
                },
                "note": {"type": "string", "description": "备注，可选", "default": ""},
            },
            "required": ["origin", "destination", "travel_date"],  # 只留 3 个必填
        },
    },
}

# --------------------------------------------------------------------------
# 版本 v2：v1 都做对了，只有 ticket_type 忘了写 enum
#
# 为什么专挑"票种"这个字段：席别、人数模型都能从用户意图里推出来，
# 唯独票种——用户没说，模型手上也没有合法值清单，只能编一个。
# 这就是笔记 4.4「用枚举收敛可选项而不是让 LLM 自由发挥」的靶心。
# --------------------------------------------------------------------------
SCHEMA_V1_NO_ENUM: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "book_train_ticket_no_enum",
        "description": "购买火车票（票种字段忘了用枚举收敛的版本）",
        "parameters": {
            "type": "object",
            "properties": {
                "origin": {"type": "string", "description": "出发站，例如 北京南"},
                "destination": {"type": "string", "description": "到达站，例如 上海虹桥"},
                "travel_date": {"type": "string", "description": "乘车日期，格式 YYYY-MM-DD"},
                "passengers": {
                    "type": "integer",
                    "default": 1,
                    "minimum": 1,
                    "maximum": MAX_PASSENGERS,
                },
                "seat_class": {
                    "type": "string",
                    "enum": KNOWN_SEAT_CLASSES,
                    "default": "二等座",
                },
                # 没有 enum、没有 default：模型只能自己编一个看上去合理的值
                "ticket_type": {"type": "string", "description": "票种"},
            },
            "required": ["origin", "destination", "travel_date", "ticket_type"],
        },
    },
}
