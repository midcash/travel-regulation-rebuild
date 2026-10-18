> 独立实验或学习材料，不是主项目开发规范。下文关于主链路的描述只对应实验起点；当前实现见 [开发说明](../../docs/development.md)。

# experiment_5_agent_notes_lab

把笔记 `07-Agent开发框架与工程实践.md` 里的 Agent 知识点，做成**能跑、能 debug** 的代码原型。

## 怎么用

每个原型都是独立的，PyCharm 里右键对应的 `main.py` → **Run / Debug** 就行。

```
python experiments/experiment_5_agent_notes_lab/<原型目录>/main.py
```

**零依赖**：全部只用 Python 标准库（`json` / `urllib` / `dataclasses` / `random` / `hashlib` …），
不需要装任何包，也不需要建 `.venv`。任何一个 Python 3.11+ 解释器都能跑。

**默认不联网**：模型调用走 mock，结果完全可复现，断点可以反复对。
想接真实模型（DeepSeek）：

```
set AGENT_NOTES_LLM=real
set DEEPSEEK_API_KEY=sk-xxx
```

## 笔记原文就在原型目录里

每个原型目录下都有一个 `笔记.md` —— 它是 `07-Agent开发框架与工程实践.md` 里
**对应章节的学习导读**：先把形状、对照表和知识点压缩到代码旁边；顶层文档仍保留完整原文。
早期原型的笔记还附带了对应原文摘录，新补的原型以完整知识点总括和代码对照为主：

```
p08_loop_graph/
├── README.md      ← 这个原型做了什么、结论是什么、踩过哪些坑
├── 笔记.md         ← 第 13 章原文（400 行）
├── main.py        ← 跑这个
├── loop_engine.py
└── graph.py
```

每个 `笔记.md` 的**最前面是「形状」**——把这一章的机制压成能一眼看完的样子：

```
# 第 13 章 · Loop Engineering & Graph工程
> 原文位置 / 对应原型 / 怎么用

## 形状
   ① 骨架        1~3 段，各 10~25 行，用**真实的标识符**（不是伪代码）
   ② 对照表      「笔记怎么说」→「代码里的真实形状」→「反直觉 / 容易漏掉」
   ③ 反直觉的点 2~4 条，每条都带 file:line，都能在调试器里验证
## 这个原型的代码              ← 本目录文件一览（从 docstring 自动提取）
---
> 以下为原文，未作改动          ← 章节原文
```

**先看「形状」，再跑 `main.py`，最后才读原文。** 「形状」那一节就是为
「懂概念但没见过代码长什么样」这种情况写的。

「反直觉的点」那一节值得单独说一句：它记的**不是笔记里的知识点**，
而是"代码实际怎么做的"和"你以为它会怎么做"之间的落差。
其中一部分是原型自己踩的坑（写下来是为了让读者别再踩），
所以那一节里会看到「原来是这样，现在改成了这样」这种写法 ——
**能被指出来的坑，比藏起来的坑有用。**

> 现在已经把 19 个章节缺口全部补成原型：第 2 章的 Function Calling 与第 4 章的
> Tool 设计原则仍合并在 `p02_function_calling`，第 4 章其余 MCP 协议内容在 `p06_mcp`。

**4.4 是个例外**：它讲的是 MCP Tool 的设计原则（入参 / 出参 / 错误 / 注解 / 粒度 / 安全），
和 p02 是同一个主题，所以摘录进了 `p02_function_calling/笔记.md` 的附录；
第 4 章其余部分是 MCP 协议本身，归 p06。

## 原型清单

| 原型 | 笔记章节 | 核心知识点 | 跑出来的东西 |
|---|---|---|---|
| [p01 ReAct 循环](p01_react_loop/) | 第 9 章 | Reason → Act → Observation → Reason | 5 个场景：单步 / 多步 / 并行工具 / 有界循环 |
| [p02 工具 Schema 设计](p02_function_calling/) | 2.2 + 4.4 | Function Calling 失败的六条对策 | 4 组 A/B 对照，差 schema 3 次尝试仍失败，好 schema 一次成功 |
| [p03 健壮性设计](p03_robustness/) | 第 17 章 | 分类 → 重试 → 熔断 → 降级 | 7 个演示：退避重试 / 熔断状态机 / 降级链 / 备用模型 / 质量退化 |
| [p04 记忆功能](p04_memory/) | 第 5 章 | 上下文压缩 + 长期记忆 + 混合检索 | 压缩 590→509、锯齿形曲线、BM25 vs 向量 7:3、按需检索省 76% token |
| [p05 Multi-Agent](p05_multi_agent/) | 第 6 章 | 五种架构 / 错误放大 / 三种通信模式 | 五架构横评表（错误放大 0.17x ~ 2.26x） |
| [p06 MCP](p06_mcp/) | 第 4 章 | 握手 / 动态发现 / 两层错误 / 传输协议 | 一个真跑的 JSON-RPC 服务端，含真实 stdio 子进程 |
| [p07 Agent 评测](p07_evaluation/) | 第 3 章 | EDD / 四层评测 / pass^k / 发布闸门 | 一个「平均分更高但被闸门拦下」的候选版本 |
| [p08 Loop / Graph 工程](p08_loop_graph/) | 第 13 章 | 五积木 + 状态文件 / 心跳 / 执行图 vs 控制图 | cron 卡死在 67%，loop 走出去；多数投票两头不讨好 |
| [p09 大模型幻觉](p09_hallucination/) | 第 10 章 | 根因 → 燃料 → 扳机 → 电路 / 七类缓解手段 | 扳机一扣幻觉翻三倍；答对和编造的置信度都是 0.950 |
| [p10 A2A](p10_a2a/) | 第 8 章 | Agent 卡片 / 任务状态机 / 与 MCP 的分工 | 一个真跑的 A2A 任务（含 input-required），内部用 p06 的 MCP |
| [p11 OpenClaw](p11_openclaw/) | 第 11 章 | 五类触发源 / Gateway 边界 / Agent / 按需记忆 / 安全 | 事件从五类入口归一化，经 Gateway 路由到 Agent，并演示记忆检索和工具拒绝 |
| [p12 Hermes 自进化](p12_hermes_self_improvement/) | 第 12 章 | 五层记忆 / 主辅模型 / 周期反思 / Skill 沉淀 | 一次任务把事实、可复用流程和用户偏好分三路持久化 |
| [p13 模型参数](p13_model_parameters/) | 第 7 章 | Temperature / Top-p / Max tokens / Penalty | 固定 logits + 固定 seed 对照稳定性、随机性和重复率 |
| [p14 Pi Harness](p14_pi_harness/) | 第 14 章 | 小内核 / JSONL / parentId 会话树 / Compaction | 回退不删历史，压缩只追加节点，展示模型上下文如何从树派生 |
| [p15 场景题](p15_agent_scenario/) | 第 15 章 | 欺诈检测 / 规则分流 / Worker / Critic / 事后反馈 | 灰区交易经过并行取证、融合、复核和风险分档 |
| [p16 Agentic 边界](p16_agentic_boundary/) | 第 16 章 | Agentic 程度 / Workflow vs Agent / 传统方法边界 | 同一请求下对照固定 Workflow、自适应 Agent 与复合错误率 |
| [p17 框架与选型](p17_framework_selection/) | 第 1 章 | 共同内核 / 六种开发形状 / 四步选型 | 对照不同团队、场景、云平台和模型偏好的候选框架 |
| [p18 上线运维](p18_online_ops/) | 第 18 章 | 业务指标 / 技术指标 / Trace / 归因 / 回滚 | 基线、退化窗口和恢复窗口的监控与修复闭环 |
| [p19 DeepSeekHarness](p19_deepseek_harness/) | 第 19 章 | Everything is a Plugin / Cordis / Fiber / 事件事实源 | 插件静态组装、HMR 重载和 append-only SessionEventLog |

## 共用零件（`common/`）

| 文件 | 作用 |
|---|---|
| `llm.py` | 模型抽象：`MockLLM`（策略函数）/ `ScriptedLLM` / `FlakyLLM` / 真实 `OpenAICompatLLM` |
| `tools.py` | 工具抽象：JSON Schema 校验、结构化错误、ToolAnnotations、`ToolRegistry` |
| `trace.py` | 打印工具，让控制台输出能读（含中日韩宽字符的表格对齐） |
| `config.py` | 类型化环境配置与启动校验 |
| `errors.py` | 可跨边界传递的错误分类、重试属性和安全错误体 |
| `resilience.py` | 指数退避、总时间预算、Bulkhead、单探针 Half-Open 熔断、同步调用超时 |
| `observability.py` | TraceContext、结构化 JSON 日志、计数器、耗时指标、敏感字段脱敏 |
| `storage.py` | fsync JSONL 事件存储与持久化幂等键（多副本时替换为 DB/Redis） |
| `security.py` | 工具 allowlist、角色检查、外部副作用审批门 |
| `runtime.py` | 每次请求的步骤/工具预算、trace 和审计出口 |

刻意保持很小 —— 每个原型都要能单独读懂，公共层一大就没人看了。

## 生产化学习层

每个 `p01` 到 `p19` 目录现在都有一个 `production.py`。它不是把业务写成大型
框架，而是把面试和上线都绕不开的边界补齐：输入校验、错误分类、超时、重试预算、
熔断、并发隔离、幂等、权限/审批、持久化事件、结构化观测和可测试的不变量。
原有文件保留为“最小原型”，便于先理解主线；阅读生产代码时优先看同目录的
`production.py`，再回看原型文件对照复杂度从哪里增加。

生产代码仍然只依赖 Python 标准库，便于单文件运行和面试讲解。`workspace/` 中生成的
JSONL 是演示用持久化；`IdempotencyStore` 对本地文件增加 OS lock，能覆盖同机多进程的 claim，
但真正多副本部署仍需把 storage/idempotency 边界替换成带唯一约束和事务的数据库或 Redis，
并把超时下沉到真实 HTTP/SDK 客户端。同步超时只是调用方截止时间，不能假装能杀掉任意 Python 线程。

## 官方开源实现参考

以下 `source/` 目录是官方仓库的浅克隆/稀疏快照，仅供阅读，不会被本实验导入或执行；
每个快照目录保留上游 LICENSE，并在对应 `source/README.md` 说明仓库、官方链接、
快照日期和重点文件。源码版权与许可证以各上游仓库为准：

| 学习目录 | 官方项目 | 重点 |
|---|---|---|
| `p11_openclaw/source/openclaw` | [OpenClaw](https://github.com/openclaw/openclaw) | Gateway、Agent Loop、工具策略、Sandbox |
| `p12_hermes_self_improvement/source/hermes-agent` | [Hermes Agent](https://github.com/NousResearch/hermes-agent) | Memory、Skill、错误分类、重试、后台反思 |
| `p14_pi_harness/source/pi-mono` | [pi-mono](https://github.com/badlogic/pi-mono) | Agent core、Session、Compaction、Prompt 组装 |
| `p19_deepseek_harness/source/deepseek-harness` | [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) | Cordis Plugin、Session/Event、可逆 effect、SDK |

这些工程共同说明：生产级 Agent 的核心不是“多写一个 while”，而是把运行时、
会话事实源、工具策略、资源预算、故障恢复、扩展生命周期和可观测性做成明确边界。

## 两条贯穿所有原型的设计约定

### 1. mock 模型用「策略函数」而不是随机数

真实模型是黑盒，学习循环结构时你需要一个**确定性的对手**，
否则每次 Run 的输出都不一样，断点根本没法看。

所以每个原型里的"模型"都是一段你能读、能打断点的 Python：

```python
def policy(messages, tools):
    if count_tool_messages(messages) == 0:
        return ToolCall("search_knowledge", {"query": "京沪高铁"})
    return "两人往返共 2212 元。"
```

### 2. 时间与随机都可注入

- 退避等待、熔断冷却用 `VirtualClock`，瞬间走完，但**每一步本来要等多久都打印出来**
- 随机数固定种子；p07 更进一步，用**按决策点独立哈希**代替连续抽样
  （连续抽样会让决策点互相耦合，导致配置里的比例和实际跑出来的不符）

## 建议的阅读顺序

如果只挑一个看，看 **p03 健壮性**（第 17 章）—— 那是笔记里最工程化、
面试被追问最多的一章，而且它的演示有完整的因果链。

如果按 Agent 主干依赖关系看：`p01`（循环骨架）→ `p02`（工具）→ `p03`（故障处理）
→ `p04`（记忆）→ `p05`（多 Agent）→ `p06`（协议）→ `p07`（评测）
→ `p08`（Loop/Graph）→ `p09`（幻觉）→ `p10`（A2A）；
再看 `p11`（事件驱动产品）→ `p12`（自进化记忆）→ `p14`（Harness 会话）→
`p19`（插件运行时）。`p13`、`p15`、`p16`、`p18` 分别是参数、场景设计、选型边界和上线运维专题。

`p09` 和 `p10` 各自独立，但 `p10` 依赖 `p06`（它要用 p06 的 MCPServer 来证明
「A2A Agent 内部用 MCP」这件事）。

每个原型目录下的 `README.md` 里都写了**建议打断点的位置**和运行方式；
如果运行结果与知识点预期不符，优先从这些断点检查中间状态。
