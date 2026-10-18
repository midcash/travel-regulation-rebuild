# p12 HermesAgent 自进化循环

对应第 12 章。Hermes 和 OpenClaw 的主干都可以理解成事件驱动 + Gateway + ReAct；本原型只突出 Hermes 的新增部分：周期性反思，把经验分三路沉淀。

## 三路沉淀

- 事实性记忆：写入 `memory`，供后续检索。
- 程序性记忆：把成功流程固化为 `Skill`；相似 Skill 走 update，不盲目 create。
- 用户建模：更新 Honcho 风格的用户偏好画像。

`ReflectionScheduler` 是代码层的稳定计数器，`AuxiliaryModel` 是轻量模型的语义判断。这个分工对应笔记中的工程结论：什么时候反思不应交给模型自己记数，值不值得保存才交给模型。

## 建议断点

- `hermes_learning.py:ReflectionScheduler.record_tool_call`：反思触发条件。
- `hermes_learning.py:AuxiliaryModel.reflect`：`Nothing to save` 和三类学习结果。
- `hermes_learning.py:LearningStore.upsert_skill`：合并已有 Skill。

运行：

```text
python experiments/experiment_5_agent_notes_lab/p12_hermes_self_improvement/main.py
```

`production.py` 的 `GovernedLearningStore` 把自进化限制为“提案—证据—审核—发布”，技能版本和审核者会落入 append-only 事件；`source/` 是 Hermes Agent 官方源码阅读快照。
