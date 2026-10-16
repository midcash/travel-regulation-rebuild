# p06 MCP（Model Context Protocol）

**对应笔记**：第 4 章（笔记里明确标了"必背"的一章）

## 为什么这个原型值得动手做

MCP 讲的是**接口协议**。协议这种东西光看对比表格记不住 ——
得让消息真的跑一遍：握手、发现、调用、报错。

所以这里写了一个**最小但完整**的 MCP，不依赖任何 SDK：

| 文件 | 作用 |
|---|---|
| `protocol.py` | JSON-RPC 2.0 的消息形状与错误码 |
| `server.py` | 服务端：`initialize` / `tools/list` / `tools/call` + 三个只读工具 |
| `client.py` | 客户端：两种传输（同进程 / 子进程 stdio） |
| `host.py` | Host：把 MCP 工具描述翻译成模型能用的 function 定义 |
| `main.py` | 7 个演示 |

## 七个演示

| 演示 | 看到了什么 |
|---|---|
| 1 握手 + 动态发现 | `initialize` 时客户端还不知道有哪些工具，`tools/list` 才知道 |
| 2 工作流 | `list_collections → describe_collection → query_knowledge` 串成一条链 |
| 3 两层错误 | 协议级（-32602）× 3 种 + 业务级（`isError: true`）× 1 种 |
| 4 动态能力更新 | 服务端热加一个工具，客户端**一行代码没改**就发现了 |
| 5 Host 翻译层 | MCP 的 `inputSchema` → 模型的 `function.parameters` |
| 6 本地 vs 远端 | 七维度对照表 + **传输协议那个面试坑** |
| 7 stdio 子进程 | 真实起一个子进程，走管道通信 |

## 四个必须看懂的点

### 1. 动态发现是 MCP 和 Function Calling 最本质的差别

演示 4 的对照组说得很直白：把 schema 硬编码在客户端，
服务端加了 `top_k` 参数，客户端根本不知道；
如果服务端是**删掉或改名**了某个参数，那就不是"用不上"，而是直接调用失败。

MCP 的做法不神奇 —— **每次用到时重新问一遍服务端**。

### 2. 两层错误，错误信息要告诉 LLM 下一步怎么办

```
协议级错误（JSON-RPC error）  "你这次请求本身不对" -> 客户端改请求就能重试
业务级错误（isError: true）   "请求没问题，但这件事办不成" -> 得换思路
```

笔记的原话：
> 返回"集合不存在，可用的集合有 A、B、C"，而不是返回 Python 异常堆栈。
> 前者 LLM 能自主修正重试，后者只能把堆栈原样输出给用户看。

演示 3 第 4 条业务错误里就带了「该集合包含这些主题：`['gold', 'platinum']`」。

### 3. 传输协议那个面试坑（演示 6）

很多人背的答案是「本地 Stdio、远端 SSE」。**这个答案会被挑战**，因为
SSE 在新版 MCP 里已经被替换了。准确的表述是：

> 新版改用无状态协议，主要通过标准 GET / POST HTTP 请求完成交互，
> SSE 降级为**可选增强**，只在需要实时推送时才启用。

### 4. 自己实现 MCP Server 的两个坑（演示 7）

1. **日志只能写 stderr。** stdout 是协议通道，混进一行非 JSON 客户端就解析失败。
2. **stdin/stdout 必须显式设成 UTF-8。** Windows 上子进程默认用本地代码页，
   写中文会编成 GBK，父进程按 UTF-8 解码直接抛 `UnicodeDecodeError`。
   这个错误**只在跨进程时出现**，同进程调试永远看不到。

## 工具设计六原则的落点（笔记 4.4）

在 `server.py` 里都能找到对应代码：

| 原则 | 落在哪 |
|---|---|
| 入参设计：必填少、默认值、enum、约束内联 | `_QUERY_SCHEMA`（必填只 2 个，`top_k` 有 default/min/max） |
| 出参设计：结构化 + LLM 友好的纯文本 | `_shape()` |
| 错误处理：双层错误 | `_tools_call()` 里 `ProtocolError` vs `BusinessError` |
| ToolAnnotations 声明副作用 | `descriptors()` 里四个标注一个不少 |
| 粒度控制：单一职责、可组合 | 三个工具形成"先发现→再了解→最后查询" |
| 安全与校验：输入校验、输出脱敏 | `_validate()` + 服务端业务层再校验一次 |

## 建议打断点的地方

1. `server.py` 的 `def handle(self, request)` —— JSON-RPC 的分发口
2. `server.py` 的 `raise ProtocolError(TOOL_NOT_FOUND, ...)` —— 协议级错误的产生点
3. `server.py` 的 `except BusinessError as exc:` —— 业务级错误的产生点
4. `client.py` 的 `response = self.transport.send(request)` —— 一次往返
5. `host.py` 的 `mcp_tools_as_functions` —— 两套字段名的翻译层

## 两种传输怎么选

```python
TRANSPORT = "in-process"   # 默认：客户端和服务端在同一个进程，断点能一路跟过去
TRANSPORT = "stdio"        # 真实形态：把 server.py 起成子进程
```

演示 7 会额外跑一遍真实的 stdio 子进程，所以不改这个常量也能看到效果。

## 怎么跑

```
python experiments/experiment_5_agent_notes_lab/p06_mcp/main.py
```

`production.py` 的 `ProductionMCPHost` 增加 bearer 鉴权、方法 allowlist、工具审批门、超时/重试和 request-id 幂等缓存，展示协议层与业务工具层的边界。
对可能产生副作用的工具，默认只允许一次尝试；只有只读或显式声明幂等的工具才会自动重试。超时后的迟到结果通过 reconciliation callback 收口到幂等记录。
