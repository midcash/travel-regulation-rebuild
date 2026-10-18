"""一个会犯错的"模型"：按 schema 构造工具参数。

为什么需要它：
    笔记 2.2 节问的是"参数一多，LLM 就容易漏必填参数，怎么避免"。
    要回答这个问题，光看结论没用，得亲眼看到模型是怎么漏的。
    真实模型的漏参是概率性的，没法在调试器里稳定复现；所以这里把"模型如何读 schema
    构造参数"写成一段确定的代码，让它犯的错可复现、可单步跟踪。

它模拟的三个真实失败模式（都能在笔记里找到对应）：
    1. 参数名没有语义（a/b/t/n）或描述为空  -> 模型映射不到意图，直接漏参
    2. 必填参数过多（>3 个）                -> 注意力被稀释，尾部必填项更容易被漏掉
    3. 没有 enum 收敛取值                    -> 模型自己发挥，传出一个业务上非法的值

以及一个真实能力：
    4. 拿到结构化错误（failed_fields + expected_schema）后，能只修这几个字段重试。
       这就是笔记 2.2 第 6 条"增加验证、反馈、重试机制"，也是 17.2.2 的落点。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from common.llm import Message, ToolCall


@dataclass
class Intent:
    """用户真正想要什么。模型的任务就是把它翻译成工具参数。"""

    origin: str = "北京南"
    destination: str = "上海虹桥"
    travel_date: str = "2026-09-20"
    passengers: int = 2
    seat_class: str = "二等座"
    note: str = ""

    def slots(self) -> dict[str, Any]:
        return {
            "origin": self.origin,
            "destination": self.destination,
            "travel_date": self.travel_date,
            "passengers": self.passengers,
            "seat_class": self.seat_class,
            "note": self.note,
        }


# 参数名里的线索词 -> 意图槽位。模型就是靠这些词去猜"这个参数该填什么"。
# 名字起得越有描述性，这张表命中得越准 —— 这就是笔记 2.2 第 4 条的价值。
SLOT_HINTS: dict[str, list[str]] = {
    "origin": ["origin", "from", "depart", "dep_", "start", "src", "出发"],
    "destination": ["destination", "dest", "to_", "arrive", "arr_", "end", "dst", "到达"],
    "travel_date": ["date", "day", "time", "when", "日期", "时间"],
    "passengers": ["passenger", "count", "num", "people", "person", "seat_count", "人数"],
    "seat_class": ["class", "seat", "grade", "level", "席别", "舱位"],
    "note": ["note", "remark", "comment", "备注"],
}

# 没有 enum 时，模型会自己发挥出来的"看起来合理但业务非法"的值，按字段名索引。
# 真实系统里这类值就是靠服务端业务校验拦下来的。
INVENTED_VALUES: dict[str, Any] = {
    "seat_class": "经济舱",  # 这是机票的说法，高铁没有这个席别
    "ticket_type": "普通票",  # 听起来很合理，但票种只有 成人票/儿童票/学生票
    "passengers": 9,
}

# 服务端错误信息里"可用 XX：[...]"那段，真实模型会读出来照抄。
# 这里把它写成一个正则，让模拟模型也具备同样的能力——否则演示的结论会失真。
_OPTIONS_RE = re.compile(r"可用[^：:]*[：:]\s*(\[[^\]]*\])")


class SimulatedModel:
    """冒充模型：给它 schema，它吐一个 ToolCall。"""

    def __init__(self, intent: Intent, verbose: bool = True):
        self.intent = intent
        self.verbose = verbose
        self.attempts: list[dict[str, Any]] = []  # 每次构造参数都记下来，方便回看

    # -- 对外入口 ---------------------------------------------------------

    def construct(self, schema: dict[str, Any], feedback: dict[str, Any] | None = None) -> ToolCall:
        """构造一次工具调用。

        feedback 是上一轮工具返回的结构化错误。有它的时候，模型只修 failed_fields
        里点名的字段——这正是结构化错误的价值：不是重来一遍，而是定点修复。
        """
        fn = schema["function"]
        name = fn["name"]
        params = fn.get("parameters", {})
        properties: dict[str, Any] = params.get("properties", {})
        required: list[str] = list(params.get("required", []))
        slots = self.intent.slots()

        # 第一步：拿参数名去猜每个字段该填什么
        mapping = {key: self._match_slot(key) for key in properties}
        arguments: dict[str, Any] = {}
        unmapped_required: list[str] = []
        for key in properties:
            slot = mapping[key]
            if slot is None:
                if key in required:
                    unmapped_required.append(key)
                # 非必填且猜不出来：这一版模型会直接不传，交给默认值兜底
                continue
            arguments[key] = slots[slot]

        # 第二步：模拟"必填参数太多导致漏参"（笔记 2.2 第 1、2 条）
        dropped_by_overload: list[str] = []
        if len(required) > 3:
            mapped_required = [k for k in required if k in arguments]
            for key in mapped_required[3:]:  # 超过 3 个之后，尾部的必填项开始丢
                arguments.pop(key, None)
                dropped_by_overload.append(key)

        # 第三步：没有 enum 约束的字段，模型会自由发挥（笔记 2.2 第 3 条）
        invented: list[str] = []
        for key, spec in properties.items():
            if "enum" in spec or key not in arguments:
                continue
            if key in INVENTED_VALUES and self._no_enum_pressure(spec):
                arguments[key] = INVENTED_VALUES[key]
                invented.append(key)

        # 第四步：有反馈时定点修复
        repaired: list[str] = []
        repaired_from_message: list[str] = []
        if feedback:
            options = self._extract_options(str(feedback.get("message", "")))
            for key in feedback.get("failed_fields", []):
                if key not in properties:
                    continue
                slot = mapping.get(key) or self._match_slot(
                    key, allow_description=True, spec=properties[key]
                )
                if slot is not None:
                    arguments[key] = self._clamp(key, slots[slot], properties[key])
                    repaired.append(key)
                elif options:
                    # 服务端把可用值列出来了，模型照抄一个——这就是"错误信息要告诉
                    # LLM 下一步该怎么办"的兑现方式（笔记 4.4 第 3 条）
                    arguments[key] = options[0]
                    repaired_from_message.append(key)
                else:
                    arguments[key] = self._pick_legal(properties[key])
                    repaired.append(key)

        record = {
            "tool": name,
            "required_count": len(required),
            "unmapped_required": unmapped_required,
            "dropped_by_overload": dropped_by_overload,
            "invented": invented,
            "repaired": repaired,
            "repaired_from_message": repaired_from_message,
        }
        self.attempts.append(record)
        if self.verbose:
            self._explain(record, arguments)
        return ToolCall(name=name, arguments=arguments)

    def chat(self, messages: list[Message], tools: list[dict[str, Any]]) -> ToolCall:
        """补一个 chat()，让它和 common.llm 的协议形状一致，方便和别的原型对照。"""
        feedback = None
        for msg in reversed(messages):
            if msg.get("role") == "tool" and "error_type" in str(msg.get("content", "")):
                try:
                    feedback = json.loads(str(msg["content"]).split("：", 1)[-1])
                except json.JSONDecodeError:
                    feedback = None
                break
        # 找到最后一条 user 消息里提到的工具名
        wanted = None
        for msg in reversed(messages):
            if msg.get("role") == "user":
                wanted = str(msg.get("content"))
                break
        for schema in tools:
            if wanted and schema["function"]["name"] in wanted:
                return self.construct(schema, feedback)
        return self.construct(tools[0], feedback)

    # -- 内部 -------------------------------------------------------------

    def _match_slot(self, key: str, allow_description: bool = False, spec: dict[str, Any] | None = None) -> str | None:
        haystack = key.lower()
        if allow_description and spec:
            haystack += " " + str(spec.get("description", "")).lower()
        for slot, hints in SLOT_HINTS.items():
            if any(hint in haystack for hint in hints):
                return slot
        return None

    @staticmethod
    def _no_enum_pressure(spec: dict[str, Any]) -> bool:
        """没有 enum、也没有默认值，模型就只能自己编一个。

        给了 default 的字段模型会照抄默认值，不会瞎编——这也是"能给默认值就给"的收益。
        """
        return "enum" not in spec and "default" not in spec and spec.get("type") in {"string", "integer"}

    @staticmethod
    def _clamp(key: str, value: Any, spec: dict[str, Any]) -> Any:
        """按 schema 的约束把值修到合法范围内。"""
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if "maximum" in spec:
                value = min(value, spec["maximum"])
            if "minimum" in spec:
                value = max(value, spec["minimum"])
        return value

    @staticmethod
    def _pick_legal(spec: dict[str, Any]) -> Any:
        if "enum" in spec:
            return spec["enum"][0]
        if "default" in spec:
            return spec["default"]
        return {"string": "未知", "integer": 1, "number": 1.0, "boolean": False}.get(spec.get("type"), "未知")

    @staticmethod
    def _extract_options(message: str) -> list[str]:
        """从「可用席别：['二等座', '一等座']」这类错误信息里把合法值抠出来。"""
        match = _OPTIONS_RE.search(message)
        if not match:
            return []
        try:
            values = json.loads(match.group(1).replace("'", '"'))
        except json.JSONDecodeError:
            return []
        return [str(v) for v in values] if isinstance(values, list) else []

    def _explain(self, record: dict[str, Any], arguments: dict[str, Any]) -> None:
        """把"模型为什么这么填"讲出来——这段就是调试时最想看的中间态。"""
        if record["unmapped_required"]:
            print(f"    [模型] 参数名看不懂，这几个必填项不敢乱填，先空着：{record['unmapped_required']}")
        if record["dropped_by_overload"]:
            print(
                f"    [模型] 必填有 {record['required_count']} 个，太多了，"
                f"后面这几个漏掉了：{record['dropped_by_overload']}"
            )
        if record["invented"]:
            print(f"    [模型] 这几个字段没有可选值清单，我自己填了：{record['invented']}")
        if record["repaired"]:
            print(f"    [模型] 按报错信息只改这几个字段：{record['repaired']}")
        if record["repaired_from_message"]:
            print(
                f"    [模型] 这几个字段我不知道合法值，从错误信息里抄了可用值："
                f"{record['repaired_from_message']}"
            )
        print(f"    [模型] 输出的参数：{json.dumps(arguments, ensure_ascii=False)}")
