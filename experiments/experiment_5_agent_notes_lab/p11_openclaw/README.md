# p11 OpenClaw 事件驱动架构

对应顶层笔记第 11 章。这个原型不复刻 OpenClaw 的 TypeScript 源码，而是把面试最常问的架构边界压成可单步调试的 Python：

`五种触发源 → Gateway 认证/路由 → Agent Think/Act → SessionTranscript`。

## 演示重点

1. `message / heartbeat / cron / hook / webhook` 都归一成同一种 `Event`。
2. `Gateway.ingest()` 只做认证、归一化和按 `session_id` 投递，不调用模型、不执行工具。
3. `OpenClawAgent` 才根据事件决定是否调用 `memory_search`，结果再写回会话。
4. 记忆采用“文件内容 + 检索索引”的两步形状；危险工具不在 allowlist 中时直接拒绝。

## 建议断点

- `openclaw_runtime.py:Gateway.ingest`：看 Gateway 的职责边界。
- `openclaw_runtime.py:OpenClawAgent.handle`：看事件如何进入 Agent。
- `openclaw_runtime.py:SecureToolRunner.call`：看权限护栏先于工具执行。
- `main.py` 的 `policy`：看“模型决策”如何由一个确定性策略模拟。

运行：

```text
python experiments/experiment_5_agent_notes_lab/p11_openclaw/main.py
```

`production.py` 的 `ProductionOpenClawRuntime` 增加事件持久化、request-id 幂等、工具注册唯一性、allowlist/审批、Bulkhead 与调用超时。`source/` 是 OpenClaw 官方源码阅读快照。
超时不是把后台 Python 线程“杀掉”：迟到成功/失败会由 reconciliation callback 写回最终幂等状态，因此重试会读取结果或明确失败，不会再次执行副作用。
