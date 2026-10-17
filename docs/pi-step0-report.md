# Pi 第 0 步验证报告

日期：2026-10-16。生产入口未切换。本报告验证接入前提，不宣称 B01–B12 或真实供应商到页面的产品验收完成。

## 结论

Pi 会话 SDK 可以从 SQLite 保存的外部 entries 注入历史消息、工具结果和压缩记录，创建完全内存会话；本项目不需要 Pi 会话文件或文件缓存。后续每轮由 SQLite 重建 AgentSession，可以保持 Node 进程常驻，不必每条消息重启进程。

采用 Pi SDK 1.3.0、Python 管理的 Windows Node 子进程可行。DeepSeek 的工具调用、文本流、推理流、缓存命中 usage、steer、取消、压缩和恢复已实际跑通。压缩与普通回答不能共用过小的输出上限；原 4,096 上限曾导致摘要不完整，最终为压缩单独使用 8,192 上限，并保留 SDK 自身更小的限制。

正式历史回放四类各三次，均完成相应协议和恢复断言；但有 14 次请求返回“无历史数据”，因此不能将 12 次正常结束解释为 12 次完整可行方案。机器可读结果已移到被忽略的 `artifacts/pi-step0/pi-step0-results.json`；原始版本可从 `legacy-pre-pi:docs/pi-step0-results.json` 取回。

## 版本、环境与复现

- 现场提交：`0b7364f02e9f89ea08e42929becc8b9cc41f88fb`，当时工作区干净。此前的上限扩大、11 月场景日期和文档均已包含其中。
- 修复后回归基线：`0b1bf2e1f766ab9d8e89f5063478c87e1984e505`。
- 验证代码封存：`5e74767`；完整 SHA 与源文件哈希见结果 JSON。本报告另行提交，全部提交在最后统一 push。
- Windows，Node 22.20.0，已有 `D:/anaconda/python.exe` 3.11.11，httpx 0.28.1；Pi 及传递依赖由 lockfile 锁定。旧 `.venv` 的 Python312 路径仍失效，未将其算作可用解释器。
- 账号 `/models` 实际返回 `deepseek-flash`、`deepseek-v4-pro`；选择前者。请求与响应型号均为 `deepseek-flash`，`thinking.type=enabled`、`reasoning_effort=low`。不修改现有 `.env`。

锁定 SDK 静态目录没有原配置的型号，验证使用内存 Provider 注册，未改写 SDK 源码。注册中的上下文容量是保守测试参数，不是官方型号规格；价格元数据没有用于账单计算，本报告不报告零费用。

安装在 `verification/pi-step0/` 中执行 `npm.cmd ci --ignore-scripts --no-audit --no-fund`。其余命令在仓库根目录执行，使用有效 Python：

```powershell
python -X utf8 -m unittest discover -s verification/pi-step0 -p test_contracts.py -v
python -X utf8 verification/pi-step0/run.py offline
python -X utf8 verification/pi-step0/run.py probe
python -X utf8 verification/pi-step0/run.py live
python -X utf8 verification/pi-step0/run.py replay
python -X utf8 verification/pi-step0/run.py replay --scenario compact_restore
python -X utf8 verification/pi-step0/report.py
```

live/probe/replay 会产生模型费用，不访问真实差旅供应商；结果在忽略的 `artifacts/pi-step0/`。历史数据目录未随包提供时回放应失败，不在线补齐。report 命令需要本轮保留的调试分组文件；普通复现可直接阅读自身 run 输出，无须重造调试失败。

## 各项验证结果

**回归基线通过。** 旧循环恢复 10 次模型、12 次工具、24,000 token，HIL 默认 3 次人工请求。固定回归保持 2026-09 日期，HTTP 测试注入固定上海时钟；不放宽 oracle。132 项 Python 回归、三项 Node UI 检查通过，baseline eval 为 6/6。页面快捷示例与结构化时间从服务端上海当前时间加三天生成，使用解析器支持的明确日期。原基线说明保存在 `legacy-pre-pi:docs/regression-baseline.md`。

**工具、流式与计量通过。** 真实模型生成工具调用；Python 提交 SQLite 工具结果后才回传 Node；模型依据结果完成文本回答。收到文本与推理增量，并从完整消息取得推理内容。原始 DeepSeek usage 具有 `prompt_tokens`、`completion_tokens`、`total_tokens`、`prompt_cache_hit_tokens`、`prompt_cache_miss_tokens`、`prompt_tokens_details.cached_tokens` 和 `completion_tokens_details.reasoning_tokens`。

Pi 的 `usage.input` 表示未命中的输入；总输入为 input + cacheRead，不能只用 input 估计成本。推理 token 已在输出计量内，不能重复相加。原始接口未提供独立 cache-write 字段，Pi 规范化 cacheWrite=0 不能当成供应商明确报告写入为零。取消可能没有最终供应商 usage，必须标为不完整，不能据 SDK 的零值推断免费。

**steer、取消与选择语义通过。** 工具等待时，Python 先写入选择 B，再发送 steer，后续真实回答确认 B。空闲时选定 B 只写库，模型调用数不变；模拟下一轮边界从 SQLite 注入 B，再重建会话。真实恢复回答保留 B 与 `evidence-hotel-B`，无新工具查询。取消在十秒内结束，无后续新工具；不承诺已经发出的模型请求或供应商请求可撤回。按钮语义在隔离后端验证，当前网页尚未接入 Pi。

**压缩和内存恢复通过。** 使用 `SessionManager.inMemory(cwd, undefined, entries)`、内存设置、凭证与模型目录、自定义 ResourceLoader，无 `.jsonl` 文件。保存真实压缩记录、选定项和证据，再用 SQLite entries 创建新的 Node 会话；正式恢复场景从当前有效状态提取 JSON 候选标识和证据 ID，分别断言正确，并断言零次新供应商查询。强制压缩测试的 keepRecentTokens=64 仅用于让短会话触发切分，不是产品配置。

**资源隔离通过。** Pi 在空临时目录运行，HOME、USERPROFILE、APPDATA、LOCALAPPDATA、Pi 配置目录重定向；加载 SDK 前挂钩 Node 文件读、目录扫描、打开、状态检查与写入口。离线审计 1,478 次访问，真实验证 1,690 次，仓库 AGENTS.md、.agents 与真实用户目录访问均未发生；两项主动违规探针均被拒绝。SDK 依赖读取不等于加载项目指令。该探针是行为回归检测，不是任意恶意代码的系统沙箱。

**Windows 与工具幂等通过。** 正常退出、强制终止、异常退出及重启均有验证。工具等待期间模拟供应商已返回但结果尚未提交，强杀实际 Node 进程；SQLite 保持 unknown，重建不自动调用模型，用户继续时生成新查询 ID。完成结果重复投递不重复调用；同 ID 不同参数被拒绝。工具请求带 Node 发出时捕获的条件版本，后续改口不能将旧请求重标为新版本；实际查询参数与结果一起保存。

**回滚读取通过。** 临时库创建旧 HIL 等待任务，新增 pi0_ 表后运行 `0b7364f` 的 HIL 读取函数，旧任务可读且旧行不变。生产数据库未迁移。2026-10-17 用户明确旧 HIL 仅本地测试数据，后续只需不修改、不删除旧表及记录，不实现新历史投影或标旧版功能；该裁决取代原只读投影要求。

**Trace 与 OTLP 通过。** Node 模型、工具结束、steer/取消、压缩事件回传 Python。最终本地 OTLP 接收端确实收到主会话 13 个 span：7 个模型、3 个工具、2 个控制、1 个根 span；恢复请求另有 2 个 span。接收端断言工具、控制、压缩标记和 token 属性存在。推理、提示词与密钥不导出 OTLP，模型与工具的 Node 耗时作为属性保留。私有本地 Trace 在 `artifacts/pi-step0/live-trace.sqlite3`；未向用户配置的外部 OTLP 服务发送本轮验证数据。

## 正式三亚回放与范围

固定当前时间为 `2026-09-10T09:00:00+08:00`，在系统提示中告知模型；查询目标为 9 月 18 日出行、19 日到会。工具只读取历史供应商响应，不把过去日期发往在线接口，也不声称复现了历史模型的 token。结果以工具名和规范化实际查询参数匹配；未返回候选不能读取其详情。数量限制应用于返回列表。HTTP 数表示历史操作包含的请求数，本轮真实供应商 HTTP 为零。

每类正式样本三次，以下为全部三次的最小值—最大值，非 p95：

- 正常：模型 4–5 次，工具 9–10 次，历史 HTTP 15 次；27,936–36,487 token；14.16–15.55 秒；历史覆盖率 90%–100%。
- 重试：模型 6–7 次，工具 11–13 次，历史 HTTP 13–18 次；35,651–47,435 token；16.52–19.76 秒；覆盖率 81.8%–92.3%。
- 改口：模型 3 次，工具 3 次，历史 HTTP 2 次；10,674–10,977 token；8.09–9.11 秒；覆盖率 66.7%。高铁缺少历史数据时明确说明，未以航班数据冒充。
- 压缩恢复：模型 7 次，工具 10 次，历史 HTTP 10–15 次；43,430–49,196 token；27.51–29.39 秒；覆盖率 60%–90%。模型次数和 token 含压缩与恢复回答。

正式合计 394,607 token、104 次到达 Python 的工具请求，其中 14 次无历史数据。覆盖率分母为到达回放边界的全部请求，缺失记录保留在分母；SDK 在执行前拒绝的无效参数不属于供应商操作，当前摘要未单独统计这一类，后续运行预算需另外计数，不能将这里的工具数当成全部模型尝试数。查询失败与无历史数据分别表达。少量候选的历史详情不齐，是回放数据覆盖限制，不证明供应商今天没有这些服务，也不能据此判断完整方案质量。

## 失败记录与复查修复

首次旧回归 131 项有 5 个日期相关失败，已在独立基线提交修复。早期探针还暴露 SQLite 连接未关闭导致 Windows 临时目录无法清理、极短会话无内容可压缩、SDK 静态目录缺少配置型号；均修复验证夹具或使用官方内存扩展接口，没有改造生产主循环。

初始压缩回放三次中一次摘要达到 4,096 输出上限而失败，未使用不完整摘要继续。独立压缩预算调整后重测，最终结构化恢复断言三次通过。初始失败仍在 JSON 的调试分组中，未计入最终成功样本。

三路独立只读复查覆盖假设、组合故障、状态串联与重复操作，修复了并行 steer 嵌套读取漏计、截断后未观察候选仍可读取详情、数量参数未应用、不同查询混计重试、旧结果版本误标、恢复断言过弱、流异常无法结束、取消和未结束请求错误标为完整用量等问题。10 项验证合同测试通过，含故意错误的候选/证据组合、未返回候选、不同查询、unknown、流异常和缺失计量。此前调试组不混入正式预算，JSON 保留其逐次摘要及排除原因。

## 重新确定的初始预算

2026-10-17 复核：394,607 token/104 工具/63 模型是四类各三次、共十二次累计。两次完整历史工具覆盖的 normal 回放为 4/5 次模型、各 9 次工具、27,936/36,487 token，含缓存命中输入。80k 也包含命中输入，本轮不调高；完整 API→真实供应商的单次三亚尚未测得，M04b 必须先校准再进入 M03b。详见 [预算核对与逐次来源](pi-budget-audit.md)。

撤回此前未经实测的 8 次模型、12 工具、64,000 token 建议。后续接入的起始预算建议为每次用户请求 **12 次模型、20 次业务工具、30 次底层 HTTP、80,000 累计 token、180 秒活跃时间**；单模型请求仍最多 45 秒。steer 不重置当前运行预算，压缩、重试和恢复后的继续均计入；新一轮请求另开预算，并保留会话累计用量。

普通回答输出上限 4,096，压缩上限 8,192，并尊重 SDK 更小的请求限制。压缩 reserveTokens=16,384；产品不能沿用测试 keepRecent=64，建议先保留约 6,000 token 近期上下文，加当前条件、选定项及证据摘要。自动压缩触发与估算应在真正长会话接入后再验收，本次实测的是显式压缩。预算前置检查需预留输出额度，无法准确计量的中断不报零费用。

80,000 约为本轮最大 49,196 的 1.6 倍，工具和 HTTP 也留有余量，但这只是单个稀疏历史案例的起始设置。它不是生产上限已经校准，也不保证所有外部失败能恢复；未知价格、低覆盖和不完整方案不能通过扩大预算掩盖。

## 后续边界

2026-10-17 裁决更新：以下两段是第 0 步结束时的旧建议，已被 [迁移方案](pi-migration-plan.md) 取代。假模型仅用于自动测试，不提供产品演示；不保留 `plan_trip` 兼容入口和旧 eval。M03a 接通开发接口，M04a/b 分开桥接回放与完整端到端计量；M03b 真实三亚成功后同批删除旧循环及旧合同，再切换默认入口。B 单按实际依赖并行。旧 HIL 仅本地测试数据，不投影到新历史，只保留旧表不改不删。

选择无凭证 Pi 内确定性假模型，使用 SDK 官方 faux provider；自检真实 Pi 循环、工具、事件、压缩、SQLite 恢复及进程生命周期，模拟 token 不进入 DeepSeek 预算。旧 baseline eval 保持独立，以检查 grader 与旧合同，不作为新业务质量证明。

plan_trip 短期保留为统一会话的一次性兼容入口；CLI serve/run/trace 保留，eval 区分旧回归与 B 场景。下一批先接 SQLite 持久化和 Pi，再保持既有行为接通成果区，再逐个放开 B 场景，最后切换与真实链路验收。本轮未接通新网页按钮、未替换旧循环/HIL、未实施生产数据迁移、未创建订单。

旧 #1–#10 按维护者裁决以 not planned 关闭，未完成真实链路验收迁入 [#11](https://github.com/midcash/travel-regulation-rebuild/issues/11)。这不是旧功能已完成验收的关闭。具体业务迁移子单需依据本报告再拆解，本轮没有提前开始产品迁移。

交付回读已逐一确认：#1–#10 均为 CLOSED / not_planned，维护者说明包含替代文档和 #11 链接，并移除 ready-for-agent、添加 wontfix；#11 为 OPEN / needs-triage。代码、报告和计量摘要统一交付到 `codex/travel-phase1`，报告提交可通过 `git log -- docs/pi-step0-report.md` 定位；不创建发布版本或部署生产服务。

官方接口依据：[Pi SDK](https://github.com/earendil-works/pi/blob/v1.3.0/packages/coding-agent/docs/sdk.md)、[外部 entries 内存会话](https://github.com/earendil-works/pi/blob/v1.3.0/packages/coding-agent/src/core/session-manager.ts)、[DeepSeek 推理与工具调用要求](https://api-docs.deepseek.com/guides/thinking_mode/)。接口文档不替代上述实测。
