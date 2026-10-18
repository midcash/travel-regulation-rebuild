> 独立实验或学习材料，不是主项目开发规范。下文关于主链路的描述只对应实验起点；当前实现见 [开发说明](../../docs/development.md)。

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

`get_policy_snapshot` 的唯一政策来源是 `policies/浙江省机关工作人员差旅费管理规定.md`。工具从原文提取文件元数据、有效日期、14 条与差旅决策直接相关的条款，并返回全部条款的定位索引；原文哈希用于确认读取的是哪一版文件。

文件职责已经收敛为四层：`config.py` 读取本实验自己的配置，`capture.py` 负责真实 API 调用和证据保存，`tools.py` 提供 7 个业务工具，`run.py` 负责日期约束、重试和流程编排。`regulation_policy.py` 只负责读取本目录下的政策原文。`run_final.py` 仅作为旧命令的兼容入口，不再承载独立逻辑。

运行方式：

```powershell
.\.venv\Scripts\python.exe -m experiments.experiment_2_hil_tools.run
```

实验二默认读取自己的 `.env`，模板见 `.env.example`，也可以用 `--env-file` 指定其他配置。结果写入 `experiments/experiment_2_hil_tools/results/<时间戳>/`。没有可用 API 密钥或供应商失败时，工具会返回显式 `error`；缺少上游候选导致的详情或路线调用会标记为 `skipped`，不伪造成功。

日期参数彼此独立：`--meeting-date` 表示会议日期，`--departure-date` 表示交通出发日期，`--check-in` 和 `--check-out` 表示住宿日期。未显式指定时，默认按会议前一天出发并入住一晚。会议时间默认为 09:00，系统按 `--arrival-buffer-minutes` 推导建议到场时间，默认为提前 30 分钟。

每个真实 API 调用默认执行 1 次初始调用和最多 3 次重试，次数可由 `--max-retries` 调整。航班和火车候选会按入住日前的到达日期过滤，次日凌晨到达的候选不会进入后续详情查询。

`search_transport` 和 `search_stays` 只返回候选摘要；只有用户选择或 Agent 判断需要深入比较时，才调用两个 detail 工具。这就是 human-in-the-loop 阶段的粗到细边界。
