# 会话恢复验收契约（Issue #42）

本目录提供会话恢复测试与合成夹具。验收范围是现有航班链路的状态持久化、同步与恢复，对应 [B08](../../docs/product-behavior.md#b08-刷新与重启后继续) 的部分能力；完整业务目标仍以现有文档为准。

## 外部约定

- 显式环境变量 `TRAVEL_SESSION_DB` 指向独立 SQLite 会话库。测试始终使用新建系统临时目录，不读取 `.env` 或用户历史数据库。正式产品的默认路径及存储内部表结构留待实现阶段确定，测试不约束具体表名。
- 测试沿用正式 `create_app(conversation)` 启动、消息、取消、新会话、读取、SSE 与关闭路径。持久化恢复后，注入的时钟和合成供应商仍可使用。
- `POST /api/messages` 的 202 表示用户消息已可靠接收；写入失败返回 503 并说明无法保存，不发起模型或供应商调用，不改变当前会话。
- `POST /api/session/reset` 的成功表示新会话指向已持久化；写入失败返回 503 并保留当前会话。旧会话数据保留，不要求本增量提供历史会话列表。
- 工具结果或最终答复的后台保存失败，运行进入 `storage_error` 并明确说明无法保存。工具结果未保存时不继续调用模型、不把结果展示成已持久化成果；此前已保存的工具结果继续保留。重新启动后以最后可靠检查点恢复，不重放失败调用。
- 已完成且内容未变化的会话重启后，`GET /api/session` 返回原快照，包括 ID、版本、消息、执行结果与时间。恢复不会凭空修改历史事实。
- 未结束的运行重启后变为 `interrupted`，状态说明包含“中断”，版本递增，运行 ID 和可靠结果保留。允许继续新轮次，原中断轮次的成果不会被新轮次擦除。不自动重放调用，也不要求自动续跑。
- SSE 与 GET 同一会话按版本防止回退；显式新会话后不接受已退出会话的迟到状态。
- 损坏数据库、未知较新 SQLite `user_version`、不可用存储路径导致明确启动失败，不静默新建空会话或覆盖原数据。

## 状态边界与保存协议

`SessionStore` 输入一个版本为 1 的检查点，输出恢复后的检查点或明确的 `StorageError`。SQLite `user_version=1`；`sessions(id,payload)` 保留各会话，`active(singleton,id)` 指向当前会话，两者在同一事务更新。正式默认路径见仓库 README；注入的测试 Conversation 默认只使用内存，显式配置路径时也经过正式存储入口。

检查点包括 `public`（浏览器快照）、`tools`（工具计数及历史）、`pi_entries`（Pi 原生会话条目）和 `pending_prompt`（已接受但尚未进入 Pi 检查点的用户输入）。Python 是保存入口；Pi 管理模型上下文；浏览器只读取 public。没有增加独立事件总线、队列或通用记忆层。

接收用户消息先提交事务，再返回 202。工具调用前预留并保存工具编号，防止中断后的调用复用旧证据 ID。可靠工具结果与对应 Pi toolResult 一起保存，再发回 Worker。每次实际模型请求前，Worker 发出上下文检查点并等待 Python 确认；轮次结束再保存最终状态。后台写入失败回退到最近可靠检查点，显示 storage_error 并停止 Worker。恢复补齐尚未进入 Pi 的用户输入；悬空工具调用只追加明确的中断错误，不伪造供应商事实。

红色提交为 `aa4c062`。实现沿用原验收条件；A5 修正协议 content 可能为文本块数组的解析，精确时间断言保持不变。测试等待上限为 95 秒，让产品现有 90 秒预算决定超时，避免较慢的 Node/Pi 冷启动被原 20 秒测试上限误判；完成、中断及调用次数断言不变。实际检查结果与浏览器证据记录在对应 PR，不把文档说明当作运行证据。

## 测试怎样提供证据

`serve_fixture.py` 启动正式 FastAPI 应用和真实 Node/Pi。模型端是本机 OpenAI 协议合成服务，供应商端是 `httpx.MockTransport`。不需要真实密钥，不访问在线模型或供应商，不产生相应费用。

合成模型根据**实际收到的历史上下文**决定是否有目的地及历史工具结果；测试还直接断言模型请求里的原消息、工具调用和关联结果。这样能区分“页面聊天记录还在”和“模型真正能接着交流”。合成模型不能证明真实模型的自然语言理解能力。

夹具的独立 JSONL 账本记录实际模型请求、供应商请求与等待节点，跨服务重启累计。测试只终止自己 `Popen` 创建的服务进程树，包括其 Node 子进程，随后用同一会话库启动新进程；没有用内存对象重建冒充服务重启。所有数据和日志均在本次临时目录，正常清理，不提交运行记录。

三个 `test_fixture_*` 是夹具对照：真实请求/工具交换、有无历史时不同的补充和回顾行为、相对日期使用实际注入时钟。在未实现恢复的主分支代码上也必须通过。其他测试的红色不能来自缺少依赖或无法启动夹具。

## 验收条件与断言入口

- A1：`test_A1_partial_conditions_and_original_model_context_survive_restart`。追问后强制终止服务、重启、仅补充条件，检查原上下文与实际工具调用。
- A2：两个 `test_A2_*`。多次重启后完整快照相等，模型具备原工具关联；价格仍未知，来源、时间、候选/证据 ID 保留，新查询的 ID 不重复。
- A3：三个 `test_A3_*`。首个模型答复前、供应商等待中、工具结果已显示但最终答复未完成时，分别强制终止进程。恢复为中断，保留可靠成果、不自动调用，能够继续新轮次；已取得的工具结果仍能用于后续回顾。
- A4：`test_A4_page_snapshot_and_sse_reconnect_have_no_side_effects` 使用真实 HTTP/SSE，刷新、重连、重启均核对快照和实际调用次数；`test_web_sync.py` 的三个测试独立验证迟到 GET、旧版本 SSE、退出会话 SSE 的竞态。
- A5：`test_A5_next_day_uses_new_clock_and_keeps_historical_result_unchanged`。隔日回顾不查供应商；新查询使用新上海时钟，历史消息和结果不变。
- A6：`test_A6_explicit_new_session_is_durable_without_deleting_old_records`。新会话指向跨重启生效，数据库逻辑导出仍有原会话和结果，新模型上下文不混入旧查询。
- A7：四个写入冲突测试、内部上下文隔离测试及 `InvalidStorageAcceptance` 三个拒绝启动测试。锁库覆盖消息接收、新会话切换、工具结果检查点和最终答复检查点；损坏、较新版本、无效路径均保留原数据并明确失败。
- A8：三个 `test_A8_*` 验证取消、串行冲突、模型/供应商失败和可靠历史结果的持久化。既有超时、Worker 崩溃/回收等回归仍由默认检查中的 [baseline](../baseline/README.md) 和 Pi 离线探针覆盖。

`web_sync_checks.mjs` 是实际渲染代码的局部单元测试，使用最小 DOM 替身，**不是实际浏览器验收**。本阶段不宣称 Agent 验收通过。

## 执行与 CI

在仓库根目录、已安装 [README](../../README.md#环境与默认检查) 中的依赖后执行：

```powershell
python -B -X utf8 scripts/check.py --suite baseline
python -B -X utf8 -m unittest discover -s verification/session-recovery -p test_recovery.py -k fixture_observes -v
python -B -X utf8 scripts/check.py --suite recovery
```

`python scripts/check.py` 默认执行全部检查。`--suite` 只用于在 CI 中把既有基线和新增恢复验收分成可见步骤；不跳过任何失败、不使用 expectedFailure，也不以零退出码隐藏红色。

[CI](../../.github/workflows/ci.yml) 对 PR、main push 和 `codex/session-state-recovery` push 触发，在 Windows、Python 3.11、Node 22.19.0 上先执行既有检查，再执行恢复契约。测试阶段预期恢复步骤失败；后续实现沿用同一套测试，核对相应提交的实际运行结果。

## 浏览器验收方法

用 Codex + Ego Lite 操作这套离线夹具的实际页面：输入条件、查询、记录卡片和调用数、终止并重启夹具、刷新/等待 SSE 恢复、继续补充或回顾、操作显式新会话。对照 `/api/session`、`/__verification__/counts` 和本次临时账本，核对消息/事实、时间、中断提示及调用次数；不得以本文或局部 DOM 检查替代实际操作证据。

真实模型及真实供应商验收需要单独估算费用和明确授权。本阶段不会运行，也不会把离线结果写成真实语言理解或完整 B08 验收通过。
