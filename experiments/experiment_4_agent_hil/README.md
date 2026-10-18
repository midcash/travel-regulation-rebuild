> 独立实验或学习材料，不是主项目开发规范。下文关于主链路的描述只对应实验起点；当前实现见 [开发说明](../../docs/development.md)。

# 实验四：Agent loop 与 human-in-the-loop 的整合

## 这个实验在验证什么

**只验证"暂停/恢复机制本身"，不验证业务逻辑。**

`runtime.py`（实验三）里，"要不要停下来问用户"是代码里写死的两个 stage 检查点；
`agent.py` 的 `plan_trip` 里，每一步调用哪个工具是模型自己决定的，但那个 for 循环
只能从头跑到尾，**没有任何暂停能力**——进程一旦退出，`messages`、`tool_results`、
`toolbox` 内部计数器、`for` 循环的 `step` 全部丢失。

本实验把 `plan_trip` 的循环体改造成"每次只推进一步、可以随时落库并退出"的形状，
并把"停下来问人"做成**模型可以自主调用的一个工具**（`request_human_input`），
而不是代码里预设的检查点。要验证的核心命题是：

> 一个由模型自主决定工具调用的循环，能不能在任意一步中断、跨进程恢复，
> 并且所有计数器、预算、配额、对话历史都不重不漏？

所以工具只有三个、业务校验只有一条。场景一复杂，出问题时就分不清
是机制的 bug 还是业务的 bug。

## 最简单的启动方式

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m experiments.experiment_4_agent_hil.cli
```

无参数进中文菜单。带子命令输出 JSON，供脚本和测试使用：

```powershell
# 在独立进程里跑到第一个暂停点
... cli start --db <路径> --script happy
# 冷启动继续
... cli resume --db <路径> --run <run_id>
# 提交人工回复
... cli respond --db <路径> --interaction <id> --action reply --text "可以"
... cli status --db <路径> --run <run_id>
... cli events --db <路径> --run <run_id>
```

`--script` 可选 `happy` / `two-asks` / `ask-forever` / `unknown-evidence` / `no-evidence`。
脚本 provider 是确定性的，不联网。

跑测试：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m unittest experiments.experiment_4_agent_hil.test_pause -v
```

## 接真实模型：模型会不会自己判断"这里该问人了"

离线用例全由脚本 provider 驱动，脚本当然会在写死的位置调用 `request_human_input`——
那只证明机制，证明不了模型有判断力。`real_run.py` 换成 DeepSeek，看模型**自主**做什么：

```powershell
# 全部三个场景（每个场景都会真的跨进程暂停一次）
... -m experiments.experiment_4_agent_hil.real_run all

# 单个场景，自己手动回复那条提问
... -m experiments.experiment_4_agent_hil.real_run one --scenario C --interactive

# 同一场景重复跑，量提问率（一次运行说明不了问题）
... -m experiments.experiment_4_agent_hil.real_run repeat --scenario B --times 4
```

三个场景按可行解个数设计，预算决定模型有没有客观依据自己拿主意：

| 场景 | 预算 | 可行组合 | 期望 |
|---|---|---|---|
| A | 420 | 1 个（早班车+远端酒店=393） | 不该问，直接 finish |
| B | 350 | 0 个（最便宜也要 393） | 该问（放宽预算？改需求？） |
| C | 600 | 2 个（393 便宜但远 / 473 方便但贵） | 该问（要哪个？） |

`all` 会把每个场景的完整对话回放、计数器和结论打印出来，明细落在 `artifacts/`。

### 已经观察到的结果

**机制：三个场景全部走通。** 真实模型下暂停、进程退出、新进程从数据库冷启动恢复、
把人工答复喂回、继续到 `finish`——包括场景 B/C 里模型连续提问的情况。这是实验四
本来要验证的命题，在真实 provider 上成立。

**模型的自主判断：不稳定，且存在系统性错误。** 各跑 4 次：

| 场景 | 期望 | 实际提问率 | 性质 |
|---|---|---|---|
| A | 不问 | 2/4 | 波动——同一条件下有时问有时不问 |
| B | 要问 | 0/4 | **系统性错误**——4 次全部宣称 `planned/feasible` |
| C | 要问 | 4/4 | 稳定正确 |

场景 B 是最值得记的一条：预算 350 元时，早班车 + 最便宜的远端酒店也要 393 元，
**一个可行组合都不存在**。模型 4 次全部返回 `finish(planned, feasible,
evidence_ids=["train_early"])`——只引用了车票，无视没有任何住宿装得下预算，
而现有校验层只查"证据是不是见过"，不查"证据够不够支撑这个结论"，所以放行了。

这条直接关系到"把该不该问用户交给模型自主判断"这个前提本身：模型有能力**使用**
人机接口（C 场景稳定正确），但在**触发**它这件事上不可靠（A 过量、B 漏报）。
可行的方向是两者结合——用确定性的前置检查兜住"必须问"（例如：观测到的证据里
不存在任何满足约束的完整组合 → 强制转入人工），把"想不想问"留给模型。
`request_human_input` 这个工具本身仍然有价值，它提供的是**通道**，不是**触发器**。

## 数据流

```
① start   建 run 行（快照：messages / step / run 计数器 / toolbox_state / elapsed_s）
② _turn   一个模型回合，三分支：
   ├─ 普通工具        执行 → 追加 tool 消息 → 回到 ②
   ├─ request_human_input  追加 assistant 消息 → 落快照 → 建 interaction
   │                       → 状态置 waiting → 返回 WAIT（进程可以退出）
   └─ finish          校验 → 终态 → 返回 DONE
③ respond 消费 + 喂回 + 复位（同一事务）→ 回到 ②
④ resume  冷启动：waiting 就返回人工请求；running 就按 pending_tool_call
          决定是"重做分派"还是"开新回合"
```

暂停点只能落在一个位置：**assistant 消息已 append、对应 tool 消息未 append 之间**。
OpenAI 风格 API 要求每个 tool_call 后面必须跟一条 `tool_call_id` 匹配的 tool 消息，
所以这是协议约束，不是设计选择。人扮演的是某一个特定 `tool_call_id` 的执行者。

## 一个 status + 一个 pending_tool_call 表达全部语义

| status | pending_tool_call | 含义 | 恢复动作 |
|---|---|---|---|
| `running` | `None` | 干净的回合边界 | 调模型 |
| `running` | 非空 | 上回合响应已落库、分派没做完 | **重做分派，不能再调模型** |
| `waiting_for_human` | 人类调用 | 等人工回复 | 返回人工请求 |
| 终态 | `None` | 结束 | 直接返回 |

第二行是最容易写错的地方：`resume` 时若无脑开新回合，历史里会出现第二个孤儿
`tool_call`，下一次加载就被不变量拦住。

## 六个用例，以及每个能抓到什么

| 用例 | 抓的是 |
|---|---|
| 1 基本闭环 | 结构可行性的下限 |
| 2 跨进程恢复 | 内存里没被复用的对象，才是真正从落库数据重建过的 |
| 3 同一次运行内多次暂停 | 对话拼接、回合计数在多轮之间错位（`step` 与 `model_calls` 分叉） |
| 4 长间隔恢复的预算 | 等待 7 天不消耗活跃预算；**剩余预算不会被重新发一份** |
| 5 幂等性 | 网络重试不会塞两条同 `tool_call_id` 的回复 |
| 6 模型问不停 | 配额跨暂停累计，不是每次恢复发一份新的 |

另有 `SnapshotInvariantTests`，把"哪类 bug 会被哪条不变量拦住"固化下来。
这些负例不是假想：**"重复喂回"在最初的实现里就是漏检的**——当时用 set 统计已回复的
`tool_call_id`，两条同 id 的回复会塌缩成一条，而重复喂回恰恰是幂等失效唯一的表现形式。
修完顺带发现更根本的问题：只要 provider 不产生 id，整个对账机制就是空的
（`Baseline` 的 `tool_call()` 就不生成 id），于是改成**落库前一律补齐 id**
（`ensure_ids`），`pending_tool_call` 也因此少了一个字段。

变异测试（人为把两处关键状态改成"不持久化"）确认用例有牙齿：

```
变异1 remaining_s 不递减        -> 失败 1 个：test_4b_budget_is_not_refilled_on_resume
变异2 人工配额不落库            -> 失败 6 个：test_2 / test_3 / test_5 / test_5b / test_6 / test_expired
```

## 文件阅读顺序

1. `snapshot.py`：快照字段定义、恢复时的不变量自检、剩余预算计算。
2. `loop.py`：`_drive` / `_turn` / `_dispatch` 三个分支。
3. `store.py`：三张表、短事务、幂等消费。
4. `verdict.py`、`tools.py`：最小业务校验层与状态外部化的工具集。
5. `provider.py`：脚本 provider（注意它为什么必须是无状态的）。
6. `test_pause.py`：六个用例 + 不变量探测器。
7. `real_run.py`：接真实模型跑，观察模型的自主介入判断。

> `.env` 是只读引用的，通过项目自带的 `travel_agent.settings.load_env()` 加载
> （白名单 + `os.environ.setdefault`，不覆盖已有变量，也不写回文件）。

## 学习取舍：本实验明确没有做的事

- **不接真实 API**。`provider.py` 默认走脚本 provider；接 DeepSeek 时整段对话
  必须始终发给同一个 provider（合成 id 的原因，见 `ensure_ids` 的注释）。
- **不接 telemetry**。`Tracer` 的 span 树跨进程必断（`telemetry.py:97` 的 `stack`
  是进程内状态，`agent.py:64` 的 `invoke_agent` span 还包着整个循环）。
  快照里保留了 `trace_id` 作为将来的关联键，但没有实现跨进程续接——
  它是一个正交的问题，混进来会让"机制的 bug"和"telemetry 的 bug"分不清。
- **不解决工具副作用的 exactly-once**。工具执行是 at-least-once：进程死在
  "工具已执行、结果未落库"之间时，恢复会重做这次调用。demo 工具对外部世界
  无副作用，重放安全；真实副作用（下单/支付）的 exactly-once 不在本实验范围。
  这是实验三 README 自己就写下的同一条取舍。
- **不做多用户权限、后台调度、并发控制**。
- **`run["duration_ms"]` 报的是活跃时长**，不是墙上时间——后者跨进程没有意义
  （`agent.py:180` 报的是墙上时间，因为那里不跨进程）。

## 与主链路的两处有意差异

1. **`status` 拆成两个概念**。`agent.py:176` 把 `run["status"]` 直接写成
   `verdict["status"]`（`planned`/`unavailable`/`needs_review`），而 `runtime.py`
   的 `status` 是运行生命周期（`completed`/`cancelled`/`expired`/`failed`）。
   本实验拆开：`snapshot["status"]` 管生命周期，`snapshot["verdict"]["status"]`
   管业务结论。这样 `TERMINAL` 集合才有意义，预算耗尽也才能同时表达
   "运行失败"和"结论是 needs_review"。
2. **`_finish` 的校验放在事务外**。`agent.py:105-110` 在 try 里做校验，
   那里只保存最终 run，没有回滚问题；本实验每个回合都要落库，
   校验抛 `ValueError` 时不该连累已经记账的那些回合。

## 接入主链路时要做的事

- 把 `verdict.validate_finish` 换成 `domain.validate_verdict`：
  它的输入（`seen` / `tool_results` / `request`）都已经在快照里。
- 给 `RealTools` 做状态外部化（`real_tools.py:21-29` 的 `calls` / `http_requests` /
  `seen` / `history` / `_transport_attempts` 目前都挂在实例上）。
- 接 telemetry 时决定 span 树怎么跨进程表达（共用一个 `trace_id`、
  每个进程段开一个新的 root span 是可行方向）。
- 工具真有外部副作用时，补上"执行前落库待执行动作 + 执行结果检查点"，
  或明确接受 at-least-once 并配人工对账。
