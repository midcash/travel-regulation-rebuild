# p15 Agent 场景题：信用卡欺诈检测

对应第 15 章。这是一个把 Agent 全流程串起来的系统设计原型：

```text
交易事件 → Gateway 认证/幂等 → 规则 Router → 灰区 Orchestrator
→ 并行 Worker → 确定性融合 → 独立 Critic → 放行/加验/拒绝/人工
→ 真实标签异步回流画像、模式库、阈值
```

## 关键取舍

- 白名单、黑名单和硬规则在 LLM 之前结束，保证秒级延迟和可审计。
- 只有灰区进入 Agent；Worker 预定义四个侦查维度，避免主 Agent 无限自由 fork。
- Worker 只返回摘要，主 Agent 不接收全部中间过程。
- 风险分由公式融合，Critic 只做独立复核；业务动作和阈值是确定性决策层。
- 高额、低置信或融合/复核分歧进入人工，不强迫模型二选一。
- 反馈必须是异步事后链路；“判定当下”没有真实标签，不能假装在线闭环已经完成。

## 建议断点

- `fraud_pipeline.py:FraudOrchestrator.classify`：规则分流、并行取证和动作分档。
- `fraud_pipeline.py:_fuse`：为什么最终分数不让 LLM 直接拍板。
- `fraud_pipeline.py:FeedbackStore.apply_label`：标签如何推动画像、模式和阈值更新。

运行：

```text
python experiments/experiment_5_agent_notes_lab/p15_agent_scenario/main.py
```

`production.py` 的 `ProductionScenarioRunner` 将并行检查结果汇聚为可解释 evidence，区分关键/非关键检查，使用 request-id 幂等和 audit JSONL 避免重复决策。
