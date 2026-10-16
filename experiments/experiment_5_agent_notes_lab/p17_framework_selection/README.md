# p17 Agent 框架与工程实践

对应顶层笔记第 1 章。框架 API 会变，但背后的问题高度相似。本原型不模拟某个框架的真实 API，而是展示：

`团队/部署硬约束 → 项目核心问题 → 框架开发形状 → 模型偏好/交付方式`。

## 六种开发形状

- 组件组装：Agent、工具、结构化输出。
- 状态图编排：State、Node、Edge。
- 角色化团队：Role、Task、协作关系。
- 环境 Harness：总体任务 + 文件/终端/浏览器/子 Agent。
- 知识管道：数据源 → 切分索引 → 检索 → Agent。
- 平台编排：模型、知识库、工具、工作流、评测、发布、日志一体化。

## 选型结论

复杂状态与恢复看 LangGraph；长任务看 DeepAgents；代码环境看 Claude Agent SDK；企业数据/RAG 看 LlamaIndex；角色型多 Agent 看 CrewAI/AgentScope；类型安全看 PydanticAI；确定的云厂商优先官方生态；低代码快速发布看 Dify。没有硬约束时，不要为了热门框架牺牲场景匹配。

## 建议断点

- `framework_selection.py:common_kernel`：框架可迁移的共同内核。
- `framework_selection.py:select`：四步选型如何体现硬约束优先。
- `main.py` 的 `methods`：六种开发形状的对应关系。

运行：

```text
python experiments/experiment_5_agent_notes_lab/p17_framework_selection/main.py
```

`production.py` 的 `ProductionFrameworkAdvisor` 用显式权重和前置假设输出选型结果，避免把单一框架包装成普适答案。
