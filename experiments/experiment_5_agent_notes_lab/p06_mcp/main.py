"""原型 06：MCP（Model Context Protocol）。对应笔记第 4 章。

MCP 是笔记里明确标了"必背"的一章。它讲的是**接口协议**，
所以光看表格记不住，得让消息真的跑一遍：握手、发现、调用、报错。

这个原型实现了一个最小但完整的 MCP：
    protocol.py  JSON-RPC 2.0 的消息形状与错误码
    server.py    服务端：initialize / tools/list / tools/call + 三个只读工具
    client.py    客户端：两种传输（同进程 / 子进程 stdio）
    host.py      Host：把 MCP 工具描述翻译成模型能用的 function 定义

运行方式（PyCharm 直接右键 Run / Debug）：
    python experiments/experiment_5_agent_notes_lab/p06_mcp/main.py

默认用**同进程传输**，这样客户端和服务端的断点都在一个进程里，单步能一路跟过去。
想看真实的 stdio 子进程形态，把 main() 里的 TRANSPORT 改成 "stdio"。

建议打断点的位置：
  - server.py 的 `def handle(self, request)`：JSON-RPC 的分发口
  - server.py 的 `raise ProtocolError(TOOL_NOT_FOUND, ...)`：协议级错误的产生点
  - server.py 的 `except BusinessError as exc:`：业务级错误的产生点
  - client.py 的 `response = self.transport.send(request)`：一次往返
  - host.py 的 `mcp_tools_as_functions`：两套字段名的翻译层
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from common import trace  # noqa: E402
from client import InProcessTransport, MCPClient, StdioTransport, ToolCallResult  # noqa: E402
from host import STATIC_HARDCODED_TOOLS, MCPToolkit, mcp_tools_as_functions  # noqa: E402
from protocol import PROTOCOL_VERSION  # noqa: E402
from server import KNOWLEDGE_BASE, MCPServer  # noqa: E402

TRANSPORT = "in-process"  # 改成 "stdio" 看真实的子进程形态


def build_client() -> MCPClient:
    server = MCPServer()
    if TRANSPORT == "stdio":
        transport = StdioTransport()
        transport.start()  # type: ignore[attr-defined]
    else:
        transport = InProcessTransport(server)
    return MCPClient(transport, name="travel-host")


def show_result(result: ToolCallResult) -> None:
    trace.kv("错误层级", result.error_kind)
    if result.protocol_error is not None:
        trace.kv("JSON-RPC code", result.protocol_error.code)
        trace.kv("message", result.protocol_error.message)
        if result.protocol_error.data:
            trace.kv("data", json.dumps(result.protocol_error.data, ensure_ascii=False)[:200])
        return
    if result.is_error:
        trace.note("业务级错误（isError: true）：")
    for line in result.text.splitlines():
        print(f"      {line}")


# ==========================================================================
# 演示 1：握手 + 动态发现
# ==========================================================================


def demo_1_handshake_and_discovery() -> MCPClient:
    trace.section(
        "演示 1：握手 + 动态发现 —— MCP 和 Function Calling 最本质的差别",
        "笔记 4.1：Function Calling 静态配置工具列表；MCP 支持工具能力的动态发现和查询",
    )
    client = build_client()
    trace.kv("传输方式", client.transport.name)

    print()
    trace.section("1-a initialize：交换协议版本和能力声明")
    info = client.initialize()
    trace.kv("协议版本", info.get("protocolVersion"))
    trace.kv("服务端能力", info.get("capabilities"))
    trace.kv("服务端信息", info.get("serverInfo"))
    trace.note(f"客户端一句话都没说工具的事 —— 它还不知道对面有什么。")
    trace.note(f"（本原型对齐的规范版本：{PROTOCOL_VERSION}）")

    print()
    trace.section("1-b tools/list：运行时才知道有哪些工具")
    tools = client.list_tools()
    trace.kv("发现工具数", len(tools))
    print()
    for tool in tools:
        annotations = tool.get("annotations", {})
        flags = [k for k, v in annotations.items() if v]
        trace.bullet(f"{tool['name']:<20} 必填={tool['inputSchema'].get('required', [])}")
        trace.bullet(f"{'':<20} 标注={flags or ['（无）']}", marker=" ")
        trace.bullet(f"{'':<20} 有 outputSchema={bool(tool.get('outputSchema'))}", marker=" ")

    print()
    trace.note("这三个工具就是笔记 4.4 第 5 条说的**粒度控制**：")
    trace.note("  list_collections -> describe_collection -> query_knowledge")
    trace.note("  形成「先发现 -> 再了解 -> 最后查询」的自然工作流，")
    trace.note("  上游的输出正好是下游的输入参数。")
    return client


# ==========================================================================
# 演示 2：完整工作流
# ==========================================================================


def demo_2_workflow(client: MCPClient) -> None:
    trace.section(
        "演示 2：三个工具串成一条工作流",
        "模型先发现有哪些集合，再了解结构，最后带着 collection 参数去查询",
    )

    steps = [
        ("list_collections", {}, "第一步：我不知道有哪些集合，先列出来"),
        ("describe_collection", {"collection": "travel_policy"}, "第二步：看看这个集合里有什么"),
        ("query_knowledge", {"collection": "travel_policy", "question": "退票要手续费吗"}, "第三步：带着集合名去查"),
        ("query_knowledge", {"collection": "member_rules", "question": "白金卡退票"}, "换个集合再查一次"),
    ]
    for name, arguments, note in steps:
        print()
        trace.section(f"tools/call  {name}", note)
        trace.kv("请求参数", json.dumps(arguments, ensure_ascii=False))
        show_result(client.call_tool(name, arguments))

    print()
    trace.note("注意 top_k 这个参数：请求里一次都没传过，服务端用默认值 2 兜住了。")
    trace.note("这就是笔记 4.4 第 1 条「能给默认值就给默认值」的收益 ——")
    trace.note("模型少填一个参数，就少一次填错的机会。")


# ==========================================================================
# 演示 3：两层错误
# ==========================================================================


def demo_3_two_layer_errors(client: MCPClient) -> None:
    trace.section(
        "演示 3：两层错误机制",
        "笔记 4.4 第 3 条：协议级用 JSON-RPC error code，业务级用 isError: true",
    )

    cases = [
        ("no_such_tool", {}, "调用一个不存在的工具 -> 协议级"),
        (
            "query_knowledge",
            {"collection": "travel_policy"},
            "缺少必填参数 question -> 协议级",
        ),
        (
            "query_knowledge",
            {"collection": "travel_policy", "question": "退票", "top_k": 99},
            "top_k 超出 schema 上限 -> 协议级",
        ),
        (
            "query_knowledge",
            {"collection": "member_rules", "question": "今天天气怎么样"},
            "参数都对，但业务上查不到 -> 业务级",
        ),
    ]
    for name, arguments, note in cases:
        print()
        trace.section(f"tools/call {name}", note)
        trace.kv("请求参数", json.dumps(arguments, ensure_ascii=False))
        show_result(client.call_tool(name, arguments))

    print()
    trace.note("为什么必须分两层（笔记 4.4 的原话）：")
    trace.note("  协议级错误说明「你这次请求本身不对」，客户端改请求就能重试；")
    trace.note("  业务级错误说明「请求没问题，但这件事办不成」，得换思路。")
    trace.note("")
    trace.note("更关键的是错误信息的写法。对比一下这两种：")
    trace.note("  ✗ 返回 Python 异常堆栈 —— LLM 只能把堆栈原样念给用户听")
    trace.note("  ✓ 「集合不存在，可用的集合有 A、B、C」—— LLM 能自主修正重试")
    trace.note("上面第 4 条业务错误里就带了「该集合包含这些主题：[...]」，")
    trace.note("模型看到这句就知道下一步该换个问法，而不是干瞪眼。")


# ==========================================================================
# 演示 4：动态能力更新 —— MCP 相对硬编码 Function Calling 的优势
# ==========================================================================


def demo_4_dynamic_capability_update(client: MCPClient) -> None:
    trace.section(
        "演示 4：服务端改了工具，客户端不用改代码",
        "笔记 4.1：MCP 的典型优点之一是动态能力更新（工具参数变更时无需重写客户端代码）",
    )

    print()
    trace.section("对照组：把 schema 硬编码在客户端的做法")
    hardcoded = STATIC_HARDCODED_TOOLS[0]["function"]
    trace.kv("客户端写死的参数", sorted(hardcoded["parameters"]["properties"]))
    live = next(t for t in client.tools if t["name"] == "query_knowledge")
    trace.kv("服务端实际的参数", sorted(live["inputSchema"]["properties"]))
    missing = sorted(set(live["inputSchema"]["properties"]) - set(hardcoded["parameters"]["properties"]))
    trace.kv("两边对不上的参数", missing)
    trace.note("硬编码的客户端不知道有 top_k。服务端加了参数，它必须改代码才能用上 ——")
    trace.note("如果服务端是**删掉或改名**了某个参数，那就不是「用不上」，而是直接调用失败。")

    print()
    trace.section("MCP 的做法：每次用到时重新发现")
    before = [t["name"] for t in client.tools]
    trace.kv("客户端当前知道的工具", before)
    trace.note("假设服务端上线了一个新工具 refund_policy_check ……")

    # 给服务端热加一个工具，然后客户端重新 tools/list
    server = getattr(client.transport, "server", None)
    if server is not None:
        original = server.descriptors

        def patched() -> list[dict]:
            from protocol import tool_descriptor

            extra = tool_descriptor(
                "refund_policy_check",
                "根据会员等级和退票时间判断是否收取手续费。",
                {
                    "type": "object",
                    "properties": {
                        "member_level": {"type": "string", "enum": ["platinum", "gold", "silver"]},
                        "hours_before_departure": {"type": "integer", "minimum": 0, "default": 24},
                    },
                    "required": ["member_level"],
                },
                annotations={"readOnlyHint": True, "idempotentHint": True},
            )
            return original() + [extra]

        server.descriptors = patched  # type: ignore[method-assign]

    after = [t["name"] for t in client.list_tools()]
    trace.kv("重新发现后", after)
    trace.kv("新增", sorted(set(after) - set(before)))
    trace.note("客户端一行代码都没改。这就是 MCP「动态能力更新」的实际样子 ——")
    trace.note("它不是魔法，就是**每次用到时重新问一遍服务端**。")

    print()
    toolkit = MCPToolkit(client)
    toolkit.refresh()
    trace.kv("Host 侧拿到的 function 定义数", len(toolkit.functions))
    for function in toolkit.functions[-1:]:
        trace.kv("最后一个", json.dumps(function["function"]["name"], ensure_ascii=False))


# ==========================================================================
# 演示 5：Host 的翻译层
# ==========================================================================


def demo_5_host_translation(client: MCPClient) -> None:
    trace.section(
        "演示 5：Host 把 MCP 描述翻译成模型能用的 function 定义",
        "笔记 4.1：Host 是运行 LLM 的应用程序",
    )
    toolkit = MCPToolkit(client)
    toolkit.refresh()

    mcp_tool = next(t for t in client.tools if t["name"] == "query_knowledge")
    function = next(f for f in toolkit.functions if f["function"]["name"] == "query_knowledge")

    print()
    trace.rule("-")
    print("  MCP tools/list 里的形状：")
    trace.rule("-")
    for line in json.dumps(mcp_tool, ensure_ascii=False, indent=2)[:600].splitlines():
        print(f"    {line}")

    print()
    trace.rule("-")
    print("  翻译成模型要的 function 定义：")
    trace.rule("-")
    for line in json.dumps(function, ensure_ascii=False, indent=2)[:600].splitlines():
        print(f"    {line}")

    print()
    trace.note("差别就三处：inputSchema 挪到 function.parameters、外面套一层 type=function、")
    trace.note("outputSchema 和 annotations 不带过去（模型用不上，但 Host 要用）。")
    trace.note("")
    trace.note("翻译完之后，模型侧完全不需要知道对面是 MCP 还是本地函数 ——")
    trace.note("这就是笔记 4.1 说的「接口标准化，减少开发成本」。")

    print()
    registry = toolkit.as_registry()
    trace.kv("转成本地工具注册表", registry.names())
    result = registry.call("query_knowledge", {"collection": "member_rules", "question": "白金卡 退票"})
    trace.kv("直接当本地工具调用", "成功" if result.ok else "失败")
    for line in result.content.splitlines()[:6]:
        print(f"      {line}")


# ==========================================================================
# 演示 6：本地 Server vs 远端 Server
# ==========================================================================


def demo_6_local_vs_remote() -> None:
    trace.section(
        "演示 6：本地 Server 和远端 Server 的差别（笔记 4.2 / 4.3）",
    )
    print()
    trace.summary_table(
        [
            {"维度": "部署位置", "本地 Server": "开发者机器上", "远端 Server": "云端或远程服务器"},
            {"维度": "连接协议", "本地 Server": "stdio（本原型默认走这条）",
             "远端 Server": "HTTP 无状态 GET/POST；SSE 已变为可选增强"},
            {"维度": "认证", "本地 Server": "通常没有鉴权机制",
             "远端 Server": "OAuth 2.0 / PKCE，支持加密与访问控制"},
            {"维度": "延迟", "本地 Server": "本地调用，实时性好", "远端 Server": "走 HTTP，相对更高"},
            {"维度": "性能占用", "本地 Server": "占本地资源，可能成为瓶颈",
             "远端 Server": "计算在远端，不影响本地 app"},
            {"维度": "扩展性", "本地 Server": "受限于本地硬件", "远端 Server": "资源在远端，扩展性强"},
            {"维度": "生态", "本地 Server": "客户端支持最好，目前用得最多",
             "远端 Server": "客户端支持不足，远程服务数量有限"},
        ],
        ["维度", "本地 Server", "远端 Server"],
    )
    print()
    trace.note("关于传输协议，这里有个**面试常考的坑**：很多人背的是「本地 Stdio、远端 SSE」，")
    trace.note("但 SSE 在新版 MCP 里已经被替换了。准确的答案是：")
    trace.note("  新版改用无状态协议，主要通过标准 GET / POST HTTP 请求完成交互，")
    trace.note("  SSE 降级为**可选增强**，只在需要实时推送时才启用。")
    trace.note("  这样降低了实现复杂度，协议更接近 REST 风格，更好集成和扩展。")
    trace.note("")
    trace.note("但要注意：本原型演示 1~5 默认**不是** stdio，而是同进程传输")
    trace.note("（文件开头的 TRANSPORT = \"in-process\"）。这不是偷懒 ——")
    trace.note("两种传输在客户端看来完全一样，因为客户端只认 transport.send(request)。")
    trace.note("同进程的好处是断点能从客户端一路跟到服务端，中间不断。")
    trace.note("")
    trace.note("想亲眼看真实的 stdio 形态，把 TRANSPORT 改成 \"stdio\"：")
    trace.note("那会把 server.py 起成一个真实子进程，用管道通信（演示 7 就是这么做的）。")


# ==========================================================================
# 演示 7：真的把 Server 起成子进程（stdio 传输）
# ==========================================================================


def demo_7_stdio_transport() -> None:
    trace.section(
        "演示 7：把 Server 起成真实子进程，走 stdio 通信",
        "笔记 4.3：本地 MCP Server 直接通过本地接口或 STDIO 通信，不涉及网络传输",
    )
    print()
    trace.note("前面六个演示走的都是同进程传输（方便打断点）。")
    trace.note("这里起一个**真实的子进程**，用管道说话 —— 这才是本地 MCP Server 的日常形态。")

    transport = StdioTransport()
    transport.start()
    client = MCPClient(transport, name="stdio-host")
    try:
        info = client.initialize()
        trace.kv("子进程 serverInfo", info.get("serverInfo"))
        tools = client.list_tools()
        trace.kv("通过管道发现的工具", [t["name"] for t in tools])

        print()
        result = client.call_tool(
            "query_knowledge", {"collection": "member_rules", "question": "白金卡退票"}
        )
        trace.kv("调用结果", "成功" if result.ok else "失败")
        for line in result.text.splitlines():
            print(f"      {line}")

        print()
        bad = client.call_tool("no_such_tool", {})
        trace.kv("错误也能原样传回来", f"code={bad.protocol_error.code} {bad.protocol_error.message}")

        print()
        trace.kv("本次往返", [(t["id"], t["method"], t["ok"]) for t in client.round_trips])
    finally:
        client.close()

    print()
    trace.kv("子进程 stderr（服务端日志）", transport.stderr_tail)
    trace.note("注意服务端日志走的是 stderr。stdout 是协议通道，")
    trace.note("混进任何一行非 JSON，客户端解析就会挂 —— 这是实现 MCP Server 的第一个坑。")
    trace.note("")
    trace.note("第二个坑就在这个演示里：**stdin/stdout 必须显式设成 UTF-8**。")
    trace.note("Windows 上子进程默认用本地代码页，写中文会编成 GBK，")
    trace.note("父进程按 UTF-8 解码直接抛 UnicodeDecodeError。")
    trace.note("这个错误只在跨进程时出现，同进程调试永远看不到。")


def main() -> None:
    trace.section(
        "原型 06：MCP（Model Context Protocol）",
        "笔记第 4 章：MCP 概念、与 Function Calling 的区别、传输协议、工具设计原则",
    )
    trace.note("MCP 是标准化接口协议，统一了 AI 模型与外部资源的连接方式。")
    trace.note("三层组件：Host（跑 LLM 的应用）/ Client / Server")

    client = demo_1_handshake_and_discovery()
    demo_2_workflow(client)
    demo_3_two_layer_errors(client)
    demo_4_dynamic_capability_update(client)
    demo_5_host_translation(client)
    demo_6_local_vs_remote()
    demo_7_stdio_transport()

    print()
    trace.section("本次会话的 JSON-RPC 往返记录")
    trace.summary_table(
        [
            {"id": trip["id"], "method": trip["method"], "结果": "成功" if trip["ok"] else "错误",
             "错误信息": trip["error"][:30]}
            for trip in client.round_trips
        ],
        ["id", "method", "结果", "错误信息"],
    )
    client.close()


if __name__ == "__main__":
    main()
