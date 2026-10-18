"""原型 02：Function Calling 与工具（Schema）设计。对应笔记 2.2 节 + 4.4 节。

一句话结论（笔记 2.2 六条建议）：参数越少越好、函数要拆、schema 要写全、
命名要有描述性、提示词要给例子、调用前要校验并把错误喂回去让模型重试。

运行方式（PyCharm 直接右键 Run / Debug 这个文件）：
    python experiments/experiment_5_agent_notes_lab/p02_function_calling/main.py

建议打断点的位置：
  - simulated_model.py 的 `arguments[key] = slots[slot]`：看模型怎么把意图翻译成参数
  - simulated_model.py 的 `if len(required) > 3:`：看必填一多是怎么漏参的
  - call_agent.py 的 `feedback = result.protocol_error`：看结构化错误怎么回到模型手里
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from common import trace  # noqa: E402
from common.tools import Tool, ToolRegistry, make_calculator_tool  # noqa: E402
from call_agent import Attempt, CallOutcome, FunctionCaller  # noqa: E402
from simulated_model import Intent, SimulatedModel  # noqa: E402
from ticketing import (  # noqa: E402
    SCHEMA_V0,
    SCHEMA_V1,
    SCHEMA_V1_NO_ENUM,
    book_train_ticket,
    book_train_ticket_no_enum,
    book_train_ticket_v0,
)

# --------------------------------------------------------------------------
# 注册表：同一个 handler 挂三个不同的名字，方便做 schema 的 A/B 对照
# --------------------------------------------------------------------------


def build_registry() -> ToolRegistry:
    registry = ToolRegistry("p02")
    common_params = dict(
        handler=book_train_ticket,
        annotations={"read_only": False, "destructive": True, "idempotent": False, "open_world": True},
    )
    registry.add(
        name="book_train_ticket_v0",
        description="买票",
        parameters=SCHEMA_V0["function"]["parameters"],
        handler=book_train_ticket_v0,
        annotations=common_params["annotations"],
    )
    registry.add(
        name="book_train_ticket",
        description=SCHEMA_V1["function"]["description"],
        parameters=SCHEMA_V1["function"]["parameters"],
        output_schema={"type": "object", "properties": {"order_id": {"type": "string"}}},
        **common_params,
    )
    registry.add(
        name="book_train_ticket_no_enum",
        description=SCHEMA_V1_NO_ENUM["function"]["description"],
        parameters=SCHEMA_V1_NO_ENUM["function"]["parameters"],
        handler=book_train_ticket_no_enum,
        annotations=common_params["annotations"],
    )
    registry.register(make_calculator_tool())
    return registry


def show_schema(schema: dict) -> None:
    """把"给 LLM 看的说明书"原样打出来——schema 写得好不好，看这段就够了。"""
    fn = schema["function"]
    params = fn["parameters"]
    trace.kv("工具名", fn["name"])
    trace.kv("描述", fn["description"])
    trace.kv("必填参数", params.get("required", []))
    print("    参数说明：")
    for key, spec in params.get("properties", {}).items():
        bits = [f"type={spec.get('type')}"]
        if "description" in spec:
            bits.append(f"desc={spec['description']}")
        if "enum" in spec:
            bits.append(f"enum={spec['enum']}")
        if "default" in spec:
            bits.append(f"default={spec['default']}")
        if "minimum" in spec:
            bits.append(f"range=[{spec.get('minimum')},{spec.get('maximum')}]")
        print(f"      {key:<14} {'  '.join(bits)}")


def print_attempt(attempt: Attempt) -> None:
    trace.rule("-")
    print(f"  第 {attempt.index} 次尝试")
    trace.kv("模型输出参数", json.dumps(attempt.tool_call.arguments, ensure_ascii=False))
    result = attempt.result
    if result.ok:
        trace.kv("工具返回", "成功")
        for line in result.content.splitlines():
            print(f"      {line}")
        return
    trace.kv("工具返回", "失败")
    payload = result.protocol_error or result.error
    trace.kv("  错误层级", "协议级（JSON-RPC error）" if result.protocol_error else "业务级（isError: true）")
    trace.kv("  结构化错误", json.dumps(payload, ensure_ascii=False))
    if payload.get("failed_fields"):
        trace.note("failed_fields 就是模型需要的修复线索 —— 没有它，模型只能原样重试")


def run_call(title: str, schema: dict, intent: Intent, max_attempts: int = 3) -> CallOutcome:
    trace.section(title)
    show_schema(schema)
    print()
    trace.note("模型开始构造参数：")
    registry = build_registry()
    model = SimulatedModel(intent, verbose=True)
    caller = FunctionCaller(model, registry, max_attempts=max_attempts)
    outcome = caller.call(schema, on_attempt=print_attempt)

    print()
    trace.rule("=")
    trace.kv("最终结果", "成功" if outcome.ok else "失败")
    trace.kv("停止原因", outcome.stopped_reason)
    trace.kv("尝试次数", f"{len(outcome.attempts)}（重试 {outcome.retries} 次）")
    return outcome


def main() -> None:
    trace.section(
        "原型 02：Function Calling 与工具 Schema 设计",
        "笔记第 2.2 节（工具调用失败问题）+ 第 4.4 节（MCPServerTool 设计原则）",
    )
    intent = Intent()
    trace.kv("用户意图（模型要把它翻译成参数）", json.dumps(intent.slots(), ensure_ascii=False))

    results = []

    # ---- 对照一：schema 没做那四条建议 ----
    results.append(
        (
            "A 差 schema",
            run_call(
                "对照 A：参数名含糊 + 7 个必填 + 无描述无默认值",
                SCHEMA_V0,
                intent,
            ),
        )
    )
    trace.note("结论：schema 没写好，重试救不回来。模型连「a 是出发站」都猜不到，")
    trace.note("      只能填一个占位值，业务校验照样拦下 —— 这是笔记 2.2 第 1~4 条的反面教材。")

    # ---- 对照二：schema 按四条建议写好 ----
    results.append(
        (
            "B 好 schema",
            run_call(
                "对照 B：三个必填 + 描述齐全 + 默认值 + enum",
                SCHEMA_V1,
                intent,
            ),
        )
    )
    trace.note("结论：一次成功。passengers / seat_class / note 都没传，靠 default 兜住了 ——")
    trace.note("      这就是「必填参数尽量少，能给默认值就给默认值」的实际收益。")

    # ---- 对照三：schema 没问题，但模型传了越界值 ----
    results.append(
        (
            "C 越界+反馈重试",
            run_call(
                "对照 C：模型传了越界的 passengers=9，靠结构化错误定点修复",
                SCHEMA_V1,
                Intent(passengers=9),
            ),
        )
    )
    trace.note("结论：failed_fields=['passengers'] + 错误里写明上限，模型才知道该改哪个字段、改成多少。")

    # ---- 对照四：没有 enum 的代价 ----
    results.append(
        (
            "D 无 enum",
            run_call(
                "对照 D：票种字段忘了写 enum，模型自由发挥出一个不存在的票种",
                SCHEMA_V1_NO_ENUM,
                intent,
            ),
        )
    )
    trace.note("结论：schema 层拦不住（它就是 string），只能业务层拦。拦下之后模型能不能自愈，")
    trace.note("      完全取决于错误信息写没写清楚可用值——而这一轮本该靠 enum 直接省掉。")
    trace.note("      所以笔记 4.4 说「用枚举收敛可选项，而不是让 LLM 自由发挥」。")

    # ---- 汇总 ----
    trace.section("四组对照结果")
    trace.summary_table(
        [
            {
                "对照": name,
                "结果": "成功" if outcome.ok else "失败",
                "尝试次数": len(outcome.attempts),
                "停止原因": outcome.stopped_reason,
            }
            for name, outcome in results
        ],
        ["对照", "结果", "尝试次数", "停止原因"],
    )

    demo_tool_annotations()


# --------------------------------------------------------------------------
# 补充：ToolAnnotations —— 让 Client 做安全决策（笔记 4.4 第 4 条）
# --------------------------------------------------------------------------


def demo_tool_annotations() -> None:
    trace.section(
        "补充：ToolAnnotations 声明副作用，让 Client 决定要不要弹确认框",
        "笔记 4.4 第 4 条；和 17.2.4「高风险操作降级为人工确认」是同一条思路",
    )

    registry = ToolRegistry("annotations")
    registry.register(make_calculator_tool())
    registry.add(
        name="query_order",
        description="查询订单状态",
        parameters={"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"]},
        handler=lambda order_id: {"order_id": order_id, "status": "已出票"},
        annotations={"read_only": True, "idempotent": True},
    )
    registry.add(
        name="refund_order",
        description="发起退款",
        parameters={
            "type": "object",
            "properties": {
                "order_id": {"type": "string"},
                "amount": {"type": "integer", "description": "退款金额（元）"},
            },
            "required": ["order_id", "amount"],
        },
        handler=lambda order_id, amount: {"refunded": True, "order_id": order_id, "amount": amount},
        annotations={"read_only": False, "destructive": True, "idempotent": False, "open_world": True},
    )

    print()
    print("  工具清单与副作用标注：")
    for tool in registry._tools.values():  # demo 里直接读内部表，方便看全貌
        print(f"    - {tool.name:<16} {tool.describe_annotations()}")

    # 模拟一个"人在回路"的确认器：退款的确认请求被拒了一次
    decisions = {"refund_order": [False, True]}

    def client_gate(name: str, arguments: dict) -> tuple[bool, str]:
        tool = registry.get(name)
        assert tool is not None
        if tool.annotations.get("read_only"):
            return True, "只读工具，自动执行"
        if tool.annotations.get("destructive"):
            queue = decisions.get(name, [True])
            approved = queue.pop(0) if queue else True
            return approved, "破坏性操作，需要用户二次确认"
        return True, "未标注副作用，按默认策略放行"

    for name, arguments in [
        ("query_order", {"order_id": "T20260920-0001"}),
        ("refund_order", {"order_id": "T20260920-0001", "amount": 553}),
        ("refund_order", {"order_id": "T20260920-0001", "amount": 553}),
    ]:
        print()
        allowed, reason = client_gate(name, arguments)
        trace.kv(f"调用 {name}", f"{'放行' if allowed else '拦截'}（{reason}）")
        if not allowed:
            trace.note("被 Client 拦下，没有发到 Server —— 标注不影响执行逻辑，但影响 Client 的信任决策")
            continue
        result = registry.call(name, arguments)
        trace.kv("执行结果", "成功" if result.ok else "失败")
        for line in result.content.splitlines():
            print(f"      {line}")


if __name__ == "__main__":
    main()
