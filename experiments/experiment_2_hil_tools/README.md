# 实验二：面向 Human-in-the-loop 的业务工具

本实验把上一轮全字段 API 观察结果收敛成 7 个 Agent 可调用的业务工具：

1. `search_transport`
2. `get_transport_detail`
3. `search_stays`
4. `get_stay_detail`
5. `resolve_place`
6. `calculate_route`
7. `get_policy_snapshot`

工具返回统一外层结构：`tool`、`status`、`data`、`evidence_refs`、`warnings`。其中 `data` 面向候选比较和用户确认，只保留决策需要的字段；原始供应商响应由 `Capture` 保存到同一次运行的 `evidence/` 目录，便于审计和排查字段映射问题。

`get_policy_snapshot` 的唯一政策来源是 `policies/浙江省机关工作人员差旅费管理规定.md`。工具从原文提取文件元数据、有效日期、14 条与差旅决策直接相关的条款，并返回全部条款的定位索引；原文哈希用于确认读取的是哪一版文件。结构化 `policy.md` 已归还实验一的 `external_tools`，实验二入口不再读取它。

运行方式：

```powershell
.\.venv\Scripts\python.exe -m experiments.experiment_2_hil_tools.run_final
```

默认会复用 `experiments/external_tools/.env`，也可以用 `--env-file` 指定配置。结果写入 `experiments/experiment_2_hil_tools/results/<时间戳>/`。没有可用 API 密钥或供应商失败时，工具会返回显式 `error`；缺少上游候选导致的详情或路线调用会标记为 `skipped`，不伪造成功。

`search_transport` 和 `search_stays` 只返回候选摘要；只有用户选择或 Agent 判断需要深入比较时，才调用两个 detail 工具。这就是 human-in-the-loop 阶段的粗到细边界。
