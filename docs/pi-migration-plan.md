# Pi 迁移方案

日期：2026-10-18。状态：迁移设计，生产入口尚未切换。依据为 [B01–B12](product-behavior.md)、[第 0 步报告](pi-step0-report.md) 和本文末尾决策记录。父工单 [#11](https://github.com/midcash/travel-regulation-rebuild/issues/11)。

最小实现是 Python 管理每个活跃会话的一个 Node 子进程，采用官方 Pi SDK 内存会话，SQLite 为唯一存储。复用现有供应商 HTTP 适配与网页结构，替换执行和交互内核。不保留长期双引擎兼容或产品假模型演示。完整迁移超过八个文件，按下述依赖分批交付。

## 第一批清理与依赖

清理前 HEAD `a23bce90349042ca0236332d342c8aeb0290fc6e` 已打 `legacy-pre-pi` 标签。检查主包、tests、第 0 步脚本及启动入口中的路径读取，再处理文档引用。

已删除 `docs/archive/`、`docs/hil-usage.md`、`docs/regression-baseline.md`、`docs/pi-step0-criteria.md`，未发现代码读取。`docs/pi-step0-results.json` 移入已忽略的 `artifacts/pi-step0/`，`verification/pi-step0/report.py:65` 改写入该位置；原始结果可从标签取回。`start-local.ps1` 删除 Provider/baseline 参数，固定 DeepSeek，该脚本原本没有独立 demo 参数。

以下因现有代码依赖延后，在引用切断前不能删除：

- **experiments/**：`verification/pi-step0/replay.py:13` 读取 `experiment_2_hil_tools/results/sanya_live_20260910T094400`，`test_contracts.py:7` 导入回放，`run.py` 的 replay 命令调用它。第二批把回放及最小脱敏夹具迁到 `tests/e2e/`，切断引用后删除整个实验目录。
- **evals/**：`travel_agent/evaluation.py:73,77` 读取 cases/gates；`tests/test_travel.py:24` 直接读取 cases；`cli.py:62`、`server.py:154` 间接调用 evaluation。第二批移除旧评测、入口与对应测试后删除。
- **travel_agent/data/scenarios.json**：`domain.py:26` 读取，`scenario_by_id` 被 `cli.py:58`、`server.py:38,215`、`evaluation.py:83`、`verification/pi-step0/run.py:121` 及旧测试调用：`test_execution_state.py`、`test_hil*.py`、`test_integration.py`、`test_loop_contract.py`、`test_policy.py`、`test_travel.py`。第二批提取测试专用夹具、删除运行时场景入口后移除。
- **CLI 内部 baseline/demo 与网页旧控件**：`cli.py:25,31`、`server.py:70–77,124–154`、`web/app.js` 和旧 UI 测试仍依赖，不能只删参数而留旧默认值。与旧引擎同批移除；第一批已去掉启动脚本暴露的选项。

引用行号对应清理前标签，实施时用符号与路径复核。不得删除用户 SQLite、真实 Trace 或密钥。AGENTS 禁读目录约束针对开发检索，运行既定测试的夹具读取是明确例外。

## 批次与依赖

第一批清理已完成。第二批拆为六个基础子工单；M03b 完成前新链路只供开发验收，**不得切换 main.py、启动脚本、默认网页或 CLI 默认执行路径**：

1. M01：Node 运行进程、协议、隔离、压缩、模型注册、计量。
2. M02：Python 桥接、SQLite、工具适配、条件版本、按钮、取消，依赖 M01 协议。
3. M03a：新 server 接口、SSE、三栏页面与 CLI 接通，依赖 M02；用显式开发入口验收，保留默认旧入口。
4. M04a：回放迁 tests/e2e、最小脱敏夹具及桥接层回放。夹具和回放代码可与 M01/M02 并行准备；完成桥接回放验收依赖 M02，所以 Blocked by M02，不依赖 M03a 的 server/SSE。
5. M04b：完整 API/SSE 端到端测试、改口补测与用量数据收集，依赖 M03a 和 M04a。
6. M03b：真实三亚验收，成功后同批删除旧引擎和 domain 旧合同、切换默认入口，依赖 M04b。

第二批保持已有真实查询与成果展示能力；第三批 B 单可以并行，不以编号先后作为依赖。M03b 不宣称 B 场景全部通过。以下箭头表示完成验收的阻塞关系，M04a 准备工作可提前进行：

```mermaid
flowchart LR
  M01 --> M02
  M02 --> M03a
  M02 --> M04a
  M03a --> M04b
  M04a --> M04b
  M04b --> M03b
  M03b --> Independent["B01、B02、B03、B04、B07、B10、B11、B12"]
  M03b --> B05
  M03b --> B08
  B05 --> B06
  B08 --> B09
```

分组内各单只依赖 M03b；B06 通过 B05、B09 通过 B08 传递依赖 M03b。

```text
网页 / CLI → Python server / 会话服务 → SQLite（唯一存储）
                         ↕ NDJSON
                   Node Pi（按会话 ID）
                         ↕ 工具请求 / 精简结果
                Python 工具适配 → 供应商
Node 事件 → Python Trace / OTLP → SSE → 网页
```

## M01：Node 运行进程

生产代码放 `runtime/pi/`，SDK 及 lockfile 沿用精确锁定的 1.3.0，不直接运行 verification worker。每个活跃 conversation ID 独立进程、AgentSession、临时目录；单会话最多一个运行，多会话不能串入消息、steer、取消或候选。空闲进程可退出；每轮用 SQLite 完整 entries（含工具消息和压缩记录）重建内存会话，不保存 Pi 会话文件。

NDJSON 信封带 conversation_id、run_id、command_id、event_id、condition_version。命令为 init/prompt/steer/cancel/snapshot/shutdown，事件包括文本增量、工具请求、模型开始/结束、压缩、控制确认、错误。stdout 只发协议，stderr 脱敏；拒绝超大帧、错误会话和同 ID 不一致重投。

Node 工具 AbortSignal 必须经 tool_cancel 传到 Python，后者停止排队及可中止 HTTP。用户取消先由 Python 落库再通知 Node；Node 崩溃/退出也取消其 Python 任务。不能撤销的在途请求保留记录，迟到结果只归档、不应用当前状态；取消后不启动新模型/HTTP，十秒未结束则终止 Node，并保留未知用量。SSE 断线只退订，不取消运行。

资源全部使用内存 SettingsManager、CredentialStore、ModelsStore、ResourceLoader，modelsPath=null、禁刷新、无默认 coding tools/skills/extensions/MCP/shell/文件工具。空临时 cwd；HOME、USERPROFILE、APPDATA、LOCALAPPDATA、Pi 配置目录重定向空目录。仅传模型凭证，供应商密钥留 Python。

隔离禁止名单包括：仓库、祖先与真实用户目录中的 AGENTS.md、CLAUDE.md、GEMINI.md、SYSTEM.md；`.agents/`、`.pi/`、`.codex/`、`.claude/`、`.gemini/`、`.cursor/`；settings/models/auth 配置、`.env*`、`.git/`、experiments、artifacts，以及真实用户目录整体。只允许锁定依赖和专用临时目录等必要运行资源。挂钩 fs/fs.promises 的读写、扫描、open/stat/access/exists/stream/realpath/readlink 及同步变体；规范化 Windows 大小写、分隔符、file URL、父路径、符号链接/联接点实际目标。测试诱饵、祖先目录、真实 home 和联接绕过，断言零访问和主动拒绝；这是回归检测，不宣称恶意代码系统沙箱。

## M02：桥接、SQLite、工具

Python 增加小型进程桥接与会话服务，复用 telemetry 和供应商模块，不自写新 Agent loop。同会话命令串行，跨会话隔离。Windows 正常退出、管道损坏、强杀与重启均回收进程和临时目录。

只新增 pi_ 表与索引，不改旧表：

- pi_conversations：ID、条件版本、条件/选定项/成果快照、状态和时间。
- pi_messages：消息 ID、会话、角色、文本、捕获的上海时间、顺序、run；客户端消息 ID 在会话内唯一，重复提交回读。
- pi_entries：SDK entry ID/parent ID、会话、顺序、完整消息或压缩 JSON、SDK 版本；逐事件事务保存，不能只在整轮成功后快照。
- pi_runs：run、会话、状态、计量与熔断原因、usage_complete、时间/取消原因；重启将残留 running 标 interrupted，不自动续费执行。
- pi_tool_calls：查询 ID、run、工具名、实际参数、捕获版本、状态、结果证据 ID、时间；同 ID 不同参数拒绝。
- pi_evidence：证据 ID、查询/候选、来源、抓取时间、实际参数、版本、精简事实及私有原始结果。
- pi_events：会话递增序号、run、类型、脱敏 payload；供 SSE 重放和 Trace 关联，Node 重投幂等。

工具先登记 pending；供应商结果和证据同事务提交后才交 Node。返回但未提交、崩溃中的调用标 unknown，重查是新查询 ID；重复已完成调用只回读结果，不查供应商。保存实际查询参数，不能仅存用户抽象条件。

工具只返回 status、必要事实/候选、evidence_ids、queried_at、condition_version、实际参数和 warnings/error；不反复注入原始 JSON/Trace/政策全文。列表必须先应用用户航司、时间等有效条件，再排序并截取最多五条，不能先截五条再筛选。供应商支持的条件下推，不支持的在已获取数据上过滤，并明确分页/覆盖限制；不擅自扩大查询或把局部无匹配说成全市场无结果。需要更多走显式分页/查询。测试匹配项位于原列表第六条以后仍可返回。详情仅接受该会话已返回候选并校验证据归属。原始数据留私有 SQLite，模型和页面按 ID 读取必要投影。

首版会话级整数条件版本。明确采纳/修改递增，比较不变更选择。页面按钮经 Python 校验候选和证据所属会话后直接事务写库，expected_version 冲突为 409；运行中 steer，空闲仅落库、下一轮注入，不调用模型。旧版本工具结果归档，以实际参数与变化字段判断复用范围，不全盘丢弃，也不覆盖新条件。

每条用户消息接收时捕获上海时间（+08:00），作为相对日期解释基准；工具执行时按当前注入时钟再次拦截过去日期，覆盖跨午夜。历史解释不发在线查询。压缩后及恢复前从 SQLite 注入当前条件、选定项、引用证据 ID、查询时间和未完成问题；模型摘要不覆盖库内状态，恢复保留 DeepSeek reasoning/tool 消息结构。

## M03a：接口、页面、CLI 接通

保留回环绑定和现有同源安全校验。新接口：

- POST /api/conversations 创建；GET /api/conversations 列历史。
- POST /api/conversations/{id}/messages 接 client_message_id/text，返回 202、message/run ID、捕获时间与版本；运行中排入 steer，不开第二个 loop。
- GET /api/conversations/{id}/events 为 SSE，支持 Last-Event-ID；先提交再发送，不输出原始推理或私密数据；过期游标提示重新读取状态。
- GET /api/conversations/{id} 返回条件、选定项、成果/证据引用、版本、运行状态、用量和一致快照内的最新事件序号，避免取状态与订阅间漏事件。
- POST /api/conversations/{id}/selection 接 candidate_id/evidence_id/expected_version，事务直写；POST /api/conversations/{id}/cancel 幂等取消。

在显式开发入口中，左侧历史改会话、中间语义交互、右侧成果接新 API/SSE，刷新可恢复；新页面不用旧轮询/HIL 表单/固定场景/演示/评测控件。CLI 接入会话服务文本请求，可带 conversation ID；serve/trace 接通新服务。无密钥明确错误，不降级假模型。M03a 不改变默认入口，不删除默认旧页面/CLI 和引擎；这些删除与默认切换归 M03b。

## M03b：真实三亚验收与同批删除

完成条件依赖 M04b：正式 API/SSE 功能测试及数据报告完成，不以用量高低作为通过条件。真实验收使用 M03a 的显式开发入口，不提前切换默认入口。

从页面提交当次未来日期的三亚出行，真实 Pi/DeepSeek/供应商完成现有去程、酒店、必要地点与接驳并进入成果区，来源/时间/证据可追查。记录提交版本、输入、Trace、截图与全部计量，在开发熔断内成功即可，不设省钱预算门禁；真实耗时如实报告。不固定历史商品或报价，不预订。缺少真实凭证、服务失败或仅回放通过，均不能切换。

成功后同批删除 agent.py、hil.py、human.py、providers.py、request_parser.py、evaluation.py、旧 tools.py/demo prompt/data、evals、scenarios 及关联测试/API/UI。先做引用闭包：real_tools 的 schema helper、供应商使用的时区/fingerprint 等抽为小型共用事实工具，不把旧模块改名保留。domain.py 拆掉 normalize_request/scenario/四段 verdict/一晚格式合同，保留确实使用的事实校验，新会话与成果校验接管。供应商合同测试保留，混合测试提取有效断言后删除旧部分。

旧 HIL 只有本地测试数据，不做新历史投影或“旧版任务”功能。不修改、不删除旧表及记录即可；新会话回滚时暂不可见，但不删新表/数据。成功后的同批删除还包含旧默认页面控件、CLI eval/scenarios/baseline/demo/structured scenario 和 plan_trip 合同，最后切换默认入口。

## M04a：桥接层回放与夹具

replay.py 迁到 tests/e2e/replay.py，保留真正引用的最小脱敏历史夹具，记录源标签/路径和哈希；不复制无关实验或私人原始数据。默认自动发现运行无凭证测试；假模型只在 tests 注册，生产 runtime 不留 fake 开关。把第 0 步有价值的协议/进程/恢复断言迁为自动测试，依赖 baseline/HIL 的兼容探针改为测试专用夹具，清理不再使用的 verification 驱动，删除 experiments 引用。

M04a 回放经过正式 Python 桥接→Pi→工具边界→SQLite，不依赖 server/SSE；不能直连 step0 worker 冒充生产桥接。夹具和协议断言可先准备，M02 接通后完成桥接验收。默认假模型 token 不当作真实计量，付费模型测试显式启用。固定时钟告知模型，未知参数返回 NoHistoricalData 并计覆盖分母；真实测试用当次未来日期。

## M04b：完整 API/SSE 与改口计量

依赖 M03a/M04a，将同一测试环境扩展为正式 server→桥接→Pi→工具边界→SQLite→SSE/状态。功能正确性仍需验证；模型/工具/HTTP/token/真实耗时只记录实际值，不以其低于某个预算作为 M04b 或 M03b 的通过条件。回放不证明真实供应商耗时，M03b 首次真实三亚须在开发熔断内成功并完整计量；熔断触发停止运行、记录原因并按缺陷排查，不以调高值替代排查。

### 完整计量与防失控熔断

开发阶段不设预算门禁。模型调用、工具尝试与实际执行、HTTP、缓存命中/未命中输入、输出、推理、摘要、重试、取消和耗时全部进入 Python Trace/OTLP 与报告汇总。累计 token 继续包含缓存命中，按模型请求累加 input+cacheRead+cacheWrite+output，并与原始 prompt_tokens+completion_tokens 交叉核验；推理已含输出，不重复相加，中断或未知用量不填零。

单次请求默认防失控熔断：模型 30 次、工具尝试 50 次（实际执行另计）、累计 token 500,000、活跃时间 10 分钟，单次模型 45 秒，均为受控配置。模型重试和摘要计入模型/用量/活跃时间，运行中 steer 不重置同一 run。HTTP 完整计数但不继承旧独立请求数预算。熔断检查在每次模型/工具前及用量返回时执行；未知用量保留标记与合理预估占用，不当零释放额度。触发即停止、记录关联 ID/阈值/实际值/原因，取消在途任务，按循环或失控缺陷排查，不在运行中自动加额，也不以单纯调高阈值替代排查。单次模型达到 45 秒也停止该 run 并记录熔断原因，不再自动重试。

每日项目总花费默认上限为人民币 50 元；价格 unknown 或不能可靠换算为人民币时，使用每日 5,000,000 累计 token（含缓存命中），两者不视为价格等值。已知金额到限仍必须停止；存在未知金额时同时用 token 控制未知风险。Python 使用 SQLite 按上海自然日汇总所有会话、进程和测试的用量，重启不重置；并发开始前原子核对/保留预计占用，结束后按实际结算，未知不填零。M01 负责逐请求计量、熔断检查、受控配置和可追溯原因；Python 桥接/SQLite 共享日累计由 M02 负责，M01 不复制 M04b 样本矩阵。

M04b 真实模型测试执行前，按样本数量、历史上下文、预计模型轮次、输出/摘要/重试估算总 token 和金额，注明假设、范围及价格来源，价格未知金额写 unknown；加上当日已用和在途占用后，预计超过对应每日上限即先停下汇报，不启动整批测试或自行减样后继续。测试过程中也持续检查日累计；这个项目日上限是防失控保护，不是以低用量评价功能质量。

### 自动压缩与数据收集

锁定 SDK 原生阈值为 contextTokens > contextWindow - reserveTokens；第 0 步关闭自动触发，未验收产品阈值。正式首版启用 SDK 原生自动压缩，不在模型流回调中调用 AgentSession.compact()（它会先 abort 当前运行）。阈值 T=min(48,000, C - 16,384)，C 为核实后的窗口，配置 SDK reserveTokens=C-T、keepRecentTokens=6,000；超过 T 时由 SDK 在安全边界压缩。这里 16,384 是最小上下文余量，实际 reserveTokens 随 C 计算，不能仍写死 16,384 而声称 48,000 触发。普通输出上限 4,096，摘要独立上限 8,192，流适配强制上限并尊重 SDK 更小限制。第 0 步 131,072 只是测试窗口，不能当官方型号规格。

压缩遵循模型上下文容量和 SDK 安全边界，不用开发预算余量作为业务验收条件。失败/截断不应用摘要，保留原历史；同一边界重复压缩、上下文仍无法容纳或失控时停止并报告缺陷，不进入压缩循环。SDK 溢出恢复、摘要和工具续轮同样计量并受熔断控制。启动要求 C 大于 16,384+6,000；这些是容量与正确性校验，不是省钱预算。

M04b 保留首轮三亚回放及三类改口数据，增加多轮后段两档：待发上下文约 30–36k、42–48k，覆盖 B05 式修改查询、B08 式重启恢复后查询、B09 式隔日当前价格核验。每档每种情境先收集一次，合计六次；可根据问题追加，不要求凑固定重复次数，也不以用量高低通过或阻塞迁移。测试前的估算需包含这六次与其他计划样本。使用相关历史消息、工具结果、选定证据及压缩记录，准备历史和后段请求分别计量；若 T 小于 48k，两档按 T 的 62.5%–75%、87.5%–100% 等比例取值。报告实际待发上下文、逐模型命中/未命中输入与输出、压缩前后规模及摘要成本、模型/工具/HTTP、重试、取消、耗时和异常；保留全部样本及失败，不筛选或补造数据。

数据供“预算优化”工单参考。待 B01–B12 完成后再据真实数据设正式预算，并通过压缩阈值、工具返回精简等手段逐步收紧。开发阶段不通过单次或样本最大值公式制定迁移门禁。

### 单一重试责任

模型网络重试仅由 Node 模型请求适配负责；SDK provider/agent 重试关闭，Python 不重发 prompt。仅 429、可恢复 5xx 或尚无流输出的连接失败最多重试一次，尊重 Retry-After、单次超时、请求熔断和日保护；一旦触发熔断不再重试。流已输出后中断不自动重试，记 usage incomplete，等待继续。认证、参数、上下文超限不重试。

供应商自动重试首版为零，由 Python 统一控制，检查并关闭 supplier_wire/httpx 隐式重试。模型看到失败后重新查询是新工具/HTTP、新 ID，照常限额；unknown 不自动重放。压缩失败无额外重试层。Trace 记录每次尝试、重试原因与次数。

### 模型价格注册与观测

使用已验证的 deepseek-flash，正式启动核对配置型号，不自动换型号。官方内存 provider 注册包含已核实 contextWindow/maxTokens、input/output/cacheRead/cacheWrite 价格、币种、每百万 token 单位、来源和生效时间。价格放非密钥版本配置，校验非负和单位，不沿用零价占位。实施时核对 [官方价格](https://api-docs.deepseek.com/quick_start/pricing) 与账号 API；若别名无对应计价依据，金额 unknown，不能声称零费用。

Node 回传原始及 Pi usage，Python Trace/OTLP 关联会话/run/request/query/version。总输入=input+cacheRead，reasoning 已含 output，不重复加；缺 cache-write 和中断最终 usage 记未知。区分模型/摘要/重试/工具/HTTP 和 usage_complete。推理仅在私有恢复 entries 保留所需结构，不向 SSE/OTLP 导出原文。

### 改口补测

已有 redirect 三次聚合数据，但缺少 steer 边界拆分、在途未知用量和所有工具尝试计量，不能证明中途改口完整成本。补“模型流式时改口”“工具在途改口”“连续两次改口”，每类先收集一次真实 DeepSeek 数据，发现问题再追加；确定性测试覆盖迟到工具、取消和重启。

逐次报告改口前后模型次数、工具尝试/执行/HTTP、输入/缓存/输出/推理、摘要 token、耗时、旧版本复用/丢弃和未知用量；关联 steer 接收/提交/消费事件及条件版本。无高铁历史数据明确缺失并计覆盖，不能拿低覆盖定产品预算；展示全部范围与失败，交给预算优化工单分析，不作为迁移预算门禁。

## 第三批：逐项 B 验收

每项独立子工单引用 product-behavior 对应段落，新增正反例并清理关联旧业务约束。B01/B02/B03/B04/B05/B07/B08/B10/B11/B12 只依赖 M03b，可并行；B06 依赖 B05（明确采纳后的修改处理），B09 依赖 B08（隔日恢复后解释历史），均传递依赖 M03b。工具/状态机制已由 M02 提供，不能仅因共享文件或编号相邻增设业务阻塞关系：

1. B01 部分查询与航司条件：解除强制完整方案前置字段。
2. B02 只追问必要字段：解除无关会议/预算/返程/住址必填。
3. B03 有理由的主要推荐与替代：选定项优先。
4. B04 同会话转向和跳过：复用 steer/版本，不恢复 HIL 阶段门。
5. B05 明确修改和冲突裁决：解除核心需求不可变约束。
6. B06 比较不采纳：假设不写选定状态。
7. B07 当天/不住/多晚/返程：清理 transport/stays/places/route 的前一日、一晚、会议角色与固定两路线限制，保留事实与衔接校验。
8. B08 刷新/重启/隔日：消息、entries、选择、成果一致恢复，不自动续费。
9. B09 历史与当前：按证据解释、按实际参数重查，刷新价格不换选择。
10. B10 制度与预算：去掉默认随包政策和默认全员适用。
11. B11 部分失败：保留可靠成果，区分失败/缺输入/无匹配，未知费用不为零。
12. B12 范围和选定含义：允许自身问答，不预订/付款/锁库存。

## 验收与回滚

清理批次运行现有 Python、三项 Node UI 和第 0 步合同测试；检查文档链接、删除路径引用、忽略目录及标签。第二批 tests/e2e 取代旧 baseline 自检，覆盖正常、认证/供应商失败、无历史数据、冲突/重复提交、崩溃、流式中断、取消、压缩失败、恢复和资源访问拒绝。

真实切换需要现有 DeepSeek、途牛、高德凭证，分别用于模型、交通住宿、地点路线，不新增账号，不输出密钥。真实三亚失败不切换。切换成功并同批删除后，可回滚 legacy-pre-pi 恢复旧程序；新增表保留，新旧用户数据都不删改。B01–B12 未逐项通过不能宣称产品全部完成。

## GitHub 子工单

- [M01 · 正式 Node 会话进程、隔离与模型计量](https://github.com/midcash/travel-regulation-rebuild/issues/12)
- [M02 · Python 桥接、SQLite 会话与精简证据工具](https://github.com/midcash/travel-regulation-rebuild/issues/13)
- [M03a · 新接口、SSE、页面三栏与 CLI 接通](https://github.com/midcash/travel-regulation-rebuild/issues/14)
- [M04a · 桥接层回放迁移与最小脱敏夹具](https://github.com/midcash/travel-regulation-rebuild/issues/15)
- [M03b · 真实三亚验收及同批删除旧引擎与 domain 旧合同](https://github.com/midcash/travel-regulation-rebuild/issues/28)
- [M04b · 完整 API/SSE 端到端测试与改口计量](https://github.com/midcash/travel-regulation-rebuild/issues/29)
- [B01 · 按当前查询交付](https://github.com/midcash/travel-regulation-rebuild/issues/16)
- [B02 · 只追问当前必要信息](https://github.com/midcash/travel-regulation-rebuild/issues/17)
- [B03 · 有理由的推荐](https://github.com/midcash/travel-regulation-rebuild/issues/18)
- [B04 · 在同一会话转向](https://github.com/midcash/travel-regulation-rebuild/issues/19)
- [B05 · 明确修改与条件冲突](https://github.com/midcash/travel-regulation-rebuild/issues/20)
- [B06 · 比较不等于采纳](https://github.com/midcash/travel-regulation-rebuild/issues/21)
- [B07 · 需要的行程形态](https://github.com/midcash/travel-regulation-rebuild/issues/22)
- [B08 · 刷新与重启后继续](https://github.com/midcash/travel-regulation-rebuild/issues/23)
- [B09 · 历史依据与当前核验](https://github.com/midcash/travel-regulation-rebuild/issues/24)
- [B10 · 制度与预算分开](https://github.com/midcash/travel-regulation-rebuild/issues/25)
- [B11 · 部分失败仍交付可靠成果](https://github.com/midcash/travel-regulation-rebuild/issues/26)
- [B12 · 业务范围与选定的含义](https://github.com/midcash/travel-regulation-rebuild/issues/27)
- [预算优化 · B01–B12 完成后设定正式预算并逐步收紧](https://github.com/midcash/travel-regulation-rebuild/issues/30)

## 决策记录

文档之间或文档与工单冲突时，以本节为准；后续新决策按日期追加，不覆盖已有记录。

### 2026-10-17：路线 B，全面采用 Pi

决定：全面采用 Pi，不保留产品 demo、旧 baseline 或长期双引擎；假模型仅用于自动测试，回滚靠 legacy-pre-pi 标签。原因：避免维护两套执行和交互合同，保留明确的旧版本恢复点。

### 2026-10-17：旧 HIL 只保留旧表

决定：旧 HIL 仅本地测试数据，旧表及记录不修改、不删除，不做新历史只读投影。原因：没有需要迁移的真实旧任务，避免为测试数据增加兼容功能。

### 2026-10-17：缓存命中计入累计 token

决定：缓存命中与未命中输入、输出均按实际用量计入累计 token，推理不重复加；中断或未知用量不填零。原因：缓存影响价格，不改变历史输入被反复提交的事实，需保持计量口径稳定。

### 2026-10-18：开发阶段完整计量与防失控熔断

决定：开发阶段不设预算门禁，只做完整计量与防失控熔断；默认单请求模型 30 次、工具 50 次、累计 50 万 token、活跃 10 分钟、单模型 45 秒，受控配置，触发即停止并按缺陷排查，不自动加额或以调高值代替排查。项目每日上限为人民币 50 元；金额 unknown 时按每日 500 万累计 token，上海自然日跨会话累计、重启不重置。真实模型测试前先估算总 token/金额（未知写 unknown），预计超过每日上限先停下汇报。M04b 多轮后段样本用于数据收集，不作预算门禁；M03b 真实三亚在熔断内成功并完整计量。正式预算由依赖 B01–B12 完成的“预算优化”工单基于真实数据设定并逐步收紧。原因：先验证通用、多轮行为并取得真实成本数据，避免用短历史样本制定过早的省钱限制，同时防止循环和失控。
