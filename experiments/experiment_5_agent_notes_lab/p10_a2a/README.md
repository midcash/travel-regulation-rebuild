# p10 A2A（Agent-to-Agent）

**对应笔记**：第 8 章

笔记这一章只有一页，它自己说明了原因：

> （根据最新面试情况，A2A 协议的概念，A2A 考的没有 MCP 那么多，
> 没有那么热，但是还是偶尔会问到。暂时问的不深，我们先记一下概念）

所以这个原型的重点不是把 A2A 讲深，而是**把它和 MCP（p06）的区别讲清楚**。

一句话：

> **MCP 让你调用工具，A2A 让你委派工作。**

## 六个演示

| 演示 | 看到了什么 |
|---|---|
| 1 Agent 卡片 | A2A 独有的「发现」这一步，以及卡片上**没有**什么 |
| 2 生命周期 | 一个任务走完 submitted → working → **input-required** → working → completed |
| 3 并排对照 | 同一件事，MCP 写一行，A2A 要写一个循环 |
| 4 内部用 MCP | **A2A Agent 内部真的通过 p06 的 MCPServer 调工具** |
| 5 内部实现不透明 | 换掉整个 Agent，Host 一行代码不用改（数字是量出来的） |
| 6 状态机的后果 | 终态拦住了「再推一下」，但重试还是会把活干两遍 |

## 核心对照表

| 对比项 | MCP（p06） | A2A（本原型） |
|---|---|---|
| 对端是什么 | 工具 / 资源 | 另一个 Agent |
| 对端的状态 | 无状态 | 有状态（一个任务跑很久） |
| 调用语义 | call → 立刻返回结果 | send → 拿到 id，之后慢慢等 |
| 中间状态 | 没有（成功 / 失败） | 8 个状态，含「等补充输入」 |
| 时间量级 | 毫秒 ~ 秒 | 分钟 ~ 天 |
| **能不能中途插话** | 不能 | **能**（input-required） |
| **重试的后果** | 通常安全（工具幂等） | **可能重复执行** |
| 发现问题 | 不需要（Server 是配好的） | agent card + 路由 |
| 返回值 | 一份数据 | 过程 + 进度 + 产物 |
| 客户端要写多少 | 一行 | 一个循环 |

最后一行不是 A2A 的缺点，是**对端在做的事本来就不同**。
一行代码能搞定的事，说明那件事本身就没有中间过程。

## 三个关键点

### 1. `input-required` 是 A2A 与 RPC 的本质分界

这是整个原型最值得看的一格状态。它意味着：

- 任务可以**停在中间**，等一句话再接着跑（不是重头来）
- 这一格在 MCP 里**没有对应物**——工具不会在中途停下来问你一个问题
- 它让 `ALLOWED_TRANSITIONS` 变成一张真表，而不是「成功/失败」两格

```
SUBMITTED        → WORKING / REJECTED / CANCELED / AUTH_REQUIRED
WORKING          → INPUT_REQUIRED / AUTH_REQUIRED / COMPLETED / FAILED / CANCELED
INPUT_REQUIRED   → WORKING / FAILED / CANCELED        ← 双向的这一格
COMPLETED        → （终态，出不去）
```

### 2. A2A 在外层，MCP 在内层（演示 4）

```
Host  ──A2A──>  差旅 Agent  ──MCP──>  知识库 / 订单系统
      （委派工作）              （调用工具）
```

这个原型里 `TravelAgent` **真的 import 了 p06 的 `MCPServer`**，
跑完一个 A2A 任务之后，它内部留下了 2 次 MCP 调用记录。

所以「A2A 是 MCP 的竞品」这个说法站不住——它们解决的是两个正交的问题：

- **MCP** 解决「Agent 怎么用上工具」——垂直方向
- **A2A** 解决「Agent 怎么跟别的 Agent 协作」——水平方向

### 3. 重试的语义不一样（演示 6）

| | MCP | A2A |
|---|---|---|
| 重试会重复什么 | 重复「读」 | 重复「做」 |
| 后果 | 浪费但无害 | 可能真的改两次签 |
| 靠什么防 | 工具自己声明 `idempotentHint` | **必须**带幂等键（messageId） |

演示里跑出来的：

```
重发同一个 messageId（不去重）: task-007 和 task-008 —— 两个任务！
重发同一个 messageId（去重）:   task-009 和 task-009 —— 同一个任务
```

笔记第 8 章说的「长时协作」，成本就藏在这些地方——
不是把请求发出去就完了，是发出去之后**还得有办法收尾**。

## 踩过的两个坑（都留在代码注释里）

1. **p06 和 p10 有同名模块，直接 import 会撞车**（`server.py` 的 `load_p06_mcp()`）
   两边都有 `protocol.py` / `client.py` / `server.py`。
   解法是按文件路径用 `importlib` **惰性**加载，并在加载期间临时把短名指向 p06 的版本。
   惰性是必须的——放模块顶层的话，p10 自己的 `server.py` 还在初始化，
   `sys.modules` 里是个半成品，临时替换会直接把 p10 的模块搞坏。

2. **加载顺序必须是依赖顺序**（同上）
   写成 `("protocol", "client", "server")` 会失败：p06 的 `client.py` 里有
   `from server import MCPServer`，加载 client 时 `sys.modules["server"]`
   还是 p10 自己那个半成品。正确的顺序是 `protocol → server → client`。

3. **`run_task` 丢掉了第一阶段的进度事件**（`client.py` 的 `run_task()`）
   `wait()` 每调一次都是新开一轮，只看最后一次的返回值，
   前面那几轮的进度就没了——而「进度」恰恰是 A2A 相对 MCP 多出来的东西。
   改成在循环里累积。

## 文件

| 文件 | 作用 |
|---|---|
| `protocol.py` | 消息 / Part / Artifact / **Task 状态机** / AgentCard |
| `server.py` | 两个 Agent（差旅改签、报销审核）+ 目录服务 + p06 桥接 |
| `client.py` | `A2AClient`：发现 → 派发 → 跟进 → 收产物 |
| `main.py` | 6 个演示 |

## 建议打断点的地方

1. `protocol.py` 的 `ALLOWED_TRANSITIONS` —— **A2A 和一次 RPC 调用的本质区别就在这张表**
2. `protocol.py` 的 `Task.transition()` —— 非法迁移是怎么被拒绝的
3. `server.py` 的 `TravelAgent._ask_preference()` —— `input-required` 是怎么产生的
4. `client.py` 的 `A2AClient.wait()` —— 异步在客户端这一侧长什么样
5. `server.py` 的 `TravelAgent._mcp()` —— **A2A Agent 内部用的是 MCP**
6. `server.py` 的 `load_p06_mcp()` —— 两个原型同名模块是怎么共存的

## 怎么跑

```
python experiments/experiment_5_agent_notes_lab/p10_a2a/main.py
```

这个原型依赖 p06（要用它的 `MCPServer`），所以**先确认 p06 能跑**。

`production.py` 的 `ProductionA2AClient` 增加安全方案检查、message_id 幂等提交、任务终态保护和取消接口；真实网络实现还需把目录、鉴权和 task store 换成远端服务。
