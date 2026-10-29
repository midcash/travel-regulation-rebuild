# Pi Durable 接入探针

结论：**迁移**。当前 V1–V6 的离线进程验收通过。此结论仅是后续迁移 issue 的输入，不修改产品运行时，也不表示用户已验收或批准合并。唯一需求来源为 [#44](https://github.com/midcash/travel-regulation-rebuild/issues/44)。#42 和 draft #43 只作参考。

`@earendil-works/pi-durable`、`@earendil-works/pi-ai`、`@earendil-works/chord` 均精确锁定 **1.4.0**，有独立 npm 锁文件。**Pi Durable 为实验性 API，可能随版本变化。** API 以本目录实际安装包的源码和类型定义为准。上游 main 和介绍文章可能使用不同接口。例如 1.4.0 的 watch 回调接收 value，工具返回 output，取消信号为 context.abortSignal。

## 复现

在仓库根目录执行，Windows 需要允许测试用 taskkill 强杀它自己启动的 Node 子进程：

```powershell
npm.cmd ci --prefix verification/durable-step0 --ignore-scripts --no-audit --no-fund
python -B -X utf8 -m unittest discover -s verification/durable-step0 -v
python -B -X utf8 scripts/check.py
```

默认检查包括原有 35 项工具测试、7 项 Pi 协议测试、21 项基线检查，以及本套件。CI 在 Windows、Node 22.19.0、Python 3.11 上由 PR 触发。本机验证使用 Windows、Node 22.20.0、Python 3.12。

每次测试都由 TemporaryDirectory 新建目录和 SQLite 库，只在该测试的重启之间复用。没有接收外部数据库路径的入口，不读取 .env、experiments 或历史库。Python 在同一把锁下读写 JSONL 调用账本，模型和工具每次调用均先记账，再让合成操作继续。`DURABLE_LEDGER` 行将相同合成证据输出到 CI 日志，不提交本机记录。子进程只继承运行所需环境变量，不继承模型凭证。Node 的 fetch、HTTP 和 socket 连接被禁止；负向测试主动尝试三个连接入口并断言全部阻止。依赖安装需要访问 npm，验收用例完全离线、模型和供应商费用为零。

## 验收结果与证据入口

所有名称位于 [test_durable.py](test_durable.py)。调用次数均为精确相等，以下数对按“模型、Python 工具”排列。

- V1 通过：`test_v1_restore_and_request_id_exactly_once`。完成后 taskkill /F，重启后的完整 entries 和会话 ID 相等；重复 requestId 返回原 submission，调用数仍为 (1, 0)。
- V2 通过：`test_v2_safe_python_bridge_and_interrupted_model`。Node 请求、Python 执行、通过 stdin 返回，与产品 RealTools 的进程桥方向一致。完成查询后 (2, 1)，下一次 Python 查询执行中强杀后 (3, 2)，恢复为 (4, 3)，只重放 interrupted 工具。再中断模型请求，从 (5, 3) 恢复到 (6, 3)。`test_v2_unsafe_tool_is_reported_without_reexecution` 验证未声明 replay 的工具最终为 (2, 1)，真实模型输入含 interrupted 诊断。
- V3 通过：`test_v3_document_survives_and_is_in_actual_model_request`。TripPlan 结构区分 selected、recommendations、history，保留未知价格和查询时间；强杀后深度相等，下一次 faux provider 收到的请求包含文档内容，调用数 (3, 1)。`test_v3_document_and_transcript_are_atomic_before_and_after_commit` 在同一批写入存储前、存储返回后分别强杀，恢复后文档和 probe.plan-change 对话记录同时不存在或同时存在，均为 (1, 1)。工具通过框架 api.commit 同时写文档和记录，不自研检查点。
- V4 通过：`test_v4_restart_before_deadline` 与 `test_v4_restart_after_deadline`。真实时间、持久化 sleepUntil、后台任务，到期前强杀后分别在到期前和到期后重启。每个场景 timer 账本精确为 1；第三次打开仍为 1，模型/工具均为 (0, 0)。任务不阻塞 idle，普通 abort 后仍 sleeping。此验证不承诺任意外部副作用在“已执行、未提交结果”间再次崩溃也恰好一次。
- V5 通过：`test_v5_network_is_denied_and_versions_are_exact` 与默认检查。只安装 faux provider，Python 工具返回固定合成事实；不调用模型网络或真实供应商，不把 skipped 当通过。
- V6 通过：`test_v6_watch_only_committed_and_sequence_survives_restart`。存储提交被拦在前侧时 watch 看不到新记录，提交后可见，强杀重启后记录不回退。subscribeCommits 的 publication.seq 单调递增，新进程第一次提交序号大于旧进程最后序号。1.4.0 的 viewState/watch 值本身**不附带 seq 或 epoch**；可用机制是框架的持久化提交 seq，后续传输层需要绑定 publication 和状态，不能直接用客户端自增序号替代。未实现前端同步。

## 四个观察项

1. **取消**：`test_observe_abort_bridge_and_invalid_storage` 观察到桥接工具 context.abortSignal 收到 abort，Python 线程仍执行，释放后 abort 完成，模型/工具保持 (1, 1)。探针选择排空已发出的 Python 请求，接近 #42 A8 的现有语义。Pi 不会跨进程自动终止 Python 的供应商操作，生产迁移仍需明确桥接层取消协议。
2. **90 秒和 45 秒**：自动续跑会重发中断调用，不能将每次进程启动当作全新执行预算。后续迁移需持久化轮次截止时间，在工具桥接处实施单次 45 秒上限，在恢复调度前比较截止时间。未改产品预算，未验证跨崩溃的生产超时策略。框架的持久化定时器不等于 HTTP 超时或供应商幂等性。
3. **无效存储**：同一观察测试在新建目录写入损坏文件或未来 durable_schema 版本，再用实际 SQLite adapter 打开，均明确拒绝，没有创建空会话。只覆盖这两种具体损坏，不声称任意 SQLite 逻辑损坏都可检测，也不声称框架会备份数据库。
4. **进程边界**：Node 唯一持有 Pi Durable SQLite；Python 持有合成工具和调用账本。生产迁移可继续让 Python 对外提供 HTTP/SSE，通过 Node IPC 取得已提交状态。本探针不接入产品 HTTP/SSE，不改变现有数据库或 Trace。

## #42 修订建议

- A3：改为自动续跑。模型中断重发，只有 replay: safe 工具可重放；其他工具向模型报告 interrupted。供应商幂等性和剩余预算另行验收。
- A4：采用已提交状态及持久化 publication.seq，或在应用中明确 epoch。刷新、重连不重复提交；将 session ID、请求代次与版本一起判断，丢弃旧会话和过期请求响应。重启恢复未完成任务本身会产生 #44 明确允许的调用，不能继续断言恢复永不调用。
- A7：针对 Pi Durable 存储定义写入失败、损坏和不兼容版本验收，保留原库并明确失败；检验提交序号与记录 ID 连续性以及前端隐私边界，不保留自研 SQLite checkpoint schema。

## 红绿交付与范围

首个测试提交 `fa6040b0345d30050bf4f4ca9b5ee444e70d8a4b` 的 [红色 Windows CI](https://github.com/midcash/travel-regulation-rebuild/actions/runs/40907106337) 已实际运行：依赖安装成功，原 63 项通过，新 10 项 assertion failures，零 errors。实现后同一套 10 项和默认 63 项本地通过；绿色 CI 及最终提交证据集中维护在 [PR #45](https://github.com/midcash/travel-regulation-rebuild/pull/45)，避免重复维护运行记录。

源码参考包括 [官方介绍](https://earendil.com/posts/pi-durable/)、[Durable README 与 examples](https://github.com/earendil-works/pi/tree/main/packages/durable)、介绍所指定 v1.0.0 的 [vacation planner](https://github.com/earendil-works/pi/tree/v1.0.0/packages/coding-agent/src/experimental/vacation) 和 [small coding agent](https://github.com/earendil-works/pi/tree/v1.0.0/packages/coding-agent/src/experimental/durable)。示例只作结构参考，API 来自本目录锁定的 1.4.0。

未验证真实模型理解、真实供应商、完整 B01–B12、生产存储迁移或多进程竞争同一库。合成模型只证明工程链路，不证明真实的语言理解。产品代码和 #43 保持原样，不合并、不关闭 issue 或 PR。
