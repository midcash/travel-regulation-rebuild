# p16 Agentic 边界与选型

对应第 16 章，专门把面试官常见的三种压力问题变成可运行对照：

- “你的 Agent 不够智能，Agentic 体现在哪里？”
- “你的系统是 Workflow，还能叫 Agent 吗？”
- “传统规则也能做，为什么要用 Agent？”

## 原型结论

Agentic 不是二元标签，而是自主性谱系。核心观察四点：模型决定下一步、模型自主选工具、工具结果形成反馈闭环、系统有完成目标；记忆、反思、自恢复和人工升级会提高 Agentic 程度。

Workflow 和 Agent 不是高低关系。路径确定、结果可验证、失败代价高时用确定性编排；路径开放、输入非结构化、需要上下文判断时才释放自主性。生产系统通常是混合形态。

## 建议断点

- `decision_modes.py:choose_architecture`：选型判据。
- `decision_modes.py:AdaptiveAgent.run`：控制流由状态改变。
- `decision_modes.py:compound_success`：多步自主性的复合错误代价。

运行：

```text
python experiments/experiment_5_agent_notes_lab/p16_agentic_boundary/main.py
```

`production.py` 的 `ProductionBoundary` 把“是否使用 Agent”变成可审计的确定性决策，并根据外部副作用和审计要求自动附加 safeguards。
