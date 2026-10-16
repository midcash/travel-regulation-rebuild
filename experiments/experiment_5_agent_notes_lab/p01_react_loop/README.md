# p01 ReAct / Agentic Loop

**对应笔记**：`07-Agent开发框架与工程实践.md` 第 9 章（ReAct）

## 这个原型想让你看见什么

笔记 9.2 给了一段伪代码，这个原型就是它的逐行落地：

```
while（任务未结束）{
    模型回答 = 调用模型（用户问题 + 对话历史 + 工具列表）
    if（模型要求调用工具）{
        工具结果 = 框架执行工具()
        将工具结果加入对话历史
        continue
    }
    // 没有工具调用，普通文本就是最终答案，结束流程
}
```

最关键的认知是**分工**：

| 谁 | 干什么 |
|---|---|
| 模型（Reasoning） | 读上下文，判断下一步做什么、做完了没有、要不要调工具 |
| 模型（Act） | 选出工具和参数 |
| **框架 / Harness** | 执行工具、把真实结果回注历史、判断何时停 |

框架本身不"智能"，它只做这四件事。所以 `react_loop.py` 里的 `ReActAgent` 只有 100 行左右。

## 文件

| 文件 | 作用 |
|---|---|
| `react_loop.py` | Harness 循环本体（`ReActAgent`），**要读的就是这个** |
| `main.py` | 5 个场景 + 每个场景的"模型策略函数" |

## 怎么跑

```
python experiments/experiment_5_agent_notes_lab/p01_react_loop/main.py
```

PyCharm：直接右键 `main.py` → Run / Debug。不联网、结果可复现。

## 五个场景

| 场景 | 循环转几圈 | 说明 |
|---|---|---|
| A 直接回答 | 1 | 模型不需要工具，第一次判断就走 else 分支结束 |
| B 单次工具 | 2 | Reason→Act→Observation→Reason→答案，ReAct 的最小完整形态 |
| C 多步 | 3 | **第二步的参数来自第一步的 Observation**，这是和"一次性生成"的分水岭 |
| D 并行工具 | 2 | 一回合吐多个 tool_call，框架要遍历执行再一起回注 |
| E 有界循环 | 4 / 2 | 模型停不下来时的两种兜底：`max_steps` 和"无进展检测" |

场景 E 对应笔记 13.1.4 的"心跳机制"——没有它，这个循环就是笔记里说的
"一台非常自信的烧钱炉"。

## 建议打断点的地方

1. `react_loop.py:104` `response = self.llm.chat(messages, tools)`
   → 看模型这一回合到底拿到了什么上下文
2. `react_loop.py:127` `result = self.registry.call(call.name, call.arguments)`
   → 看框架怎么执行工具
3. `react_loop.py:129` `messages.append(result.to_message(call.id, call.name))`
   → **最重要的一行**：Observation 是怎么变成对话历史的一部分的
4. `main.py` 里任意一个 `policy` 函数
   → 看"模型"是怎么根据已有观察决定下一步的

## 接真实模型

```
set AGENT_NOTES_LLM=real
set DEEPSEEK_API_KEY=sk-xxx
```

真实模型下 `main.py` 里的策略函数会被忽略（走 `common/llm.py` 的 `OpenAICompatLLM`），
此时"模型怎么决策"就真的交给模型了——对比一下 mock 的输出，能直观感受到
Harness 和模型的边界在哪里。

## 生产版

`production.py` 的 `ProductionReActRunner` 增加了请求预算、模型超时与重试、熔断、并发隔离、工具 allowlist、重复调用检测和结构化失败结果。原文件保留用于理解最小 ReAct 形状。
