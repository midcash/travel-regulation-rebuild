# p18 Agent 项目上线问题

对应第 18 章。这个原型把上线后的三个高频问题串起来：

```text
Trace → 业务指标/技术指标 → 相对基线告警 → 失败阶段归因 → 修复/回滚 → 新样本回归
```

## 监控什么

- 业务：任务通过率、用户正向反馈、人工升级率、工具调用成功率。
- 技术：P95 延迟、Token、单次成本、超时、429/5xx、熔断和 fallback。
- 安全：提示词注入成功率、越权、高风险动作确认漏失；安全项不能混进平均分。

原型故意把第 7 天做成“通过率下降 + 延迟上升 + 工具失败 + 安全违规”同时出现，展示为什么要保留完整执行轨迹，并按阶段归因。

## 建议断点

- `ops_monitor.py:Monitor.measure`：从 trace 计算窗口指标。
- `ops_monitor.py:Monitor.check`：业务闸门和线上告警。
- `ops_monitor.py:Attribution.diagnose`：按失败阶段选择修复方向。

运行：

```text
python experiments/experiment_5_agent_notes_lab/p18_online_ops/main.py
```

`production.py` 的 `ProductionOpsReport` 计算错误率和 P95 延迟，零流量 fail-closed，并对输出标签做敏感字段脱敏。
