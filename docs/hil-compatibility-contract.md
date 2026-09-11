# HIL 主链路兼容约定

日期：2026-09-11

状态：用户已于 2026-09-11 授权按本文分阶段开发，且每阶段验收后单独 Git commit。技术建议作为首版实现取值，实际完成情况另见阶段验收记录；授权不表示代码已实现或验收已通过。本文依据当前主链路和既有真实工具规格制定，实验四仅作为机制参考。远程工单不自动修改。

## 1. 目标与依据

将主链路扩展为可持久化暂停、接收人工答复并恢复的执行过程，同时使现有非交互调用、已接入工具及后续真实 API 工单有稳定的依赖边界。HIL 不改变“前一日抵达、一晚住宿、准时到会、有本次证据”的业务目标。

依据：

- [真实工具接入规格](real-tools-integration-spec.md)及[父工单 #1](https://github.com/midcash/travel-regulation-rebuild/issues/1)。
- [plan_trip](../travel_agent/agent.py)、[真实工具入口](../travel_agent/real_tools.py)、[领域校验](../travel_agent/domain.py)。
- [服务端](../travel_agent/server.py)、[CLI](../travel_agent/cli.py)、[评测](../travel_agent/evaluation.py)、[遥测存储](../travel_agent/telemetry.py)。
- [实验四循环](../experiments/experiment_4_agent_hil/loop.py)、[快照](../experiments/experiment_4_agent_hil/snapshot.py)、[存储](../experiments/experiment_4_agent_hil/store.py)。
- 既有 tests/test_travel.py、test_policy.py、test_transport.py、test_integration.py 中适用的外部行为断言。当前存在未跟踪的 tests/test_transport_detail.py；其内容不是已完成工单的证明，本次不修改。

父规格目前保留原循环，并排除通用多轮人工确认系统及数据库迁移。HIL 应作为单独增量：明确允许执行状态持久化、交互入口和必要展示；不因此授权通用工作流框架、页面重做或修改政策。原真实工具规格的业务验收仍有效。

## 2. 首版产品边界：用户已确认

用户于 2026-09-11 确认：“先支持候选选择、确认和偏好补充；核心需求变更新建任务”。据此，首版支持不改变核心请求的人工输入；同次任务允许多次人工交互，受配额约束。核心请求包括城市、会议地点文本、会议日期时间、交通方式和住宿要求。核心请求变化必须走新建任务流程，不在旧任务上偷偷修改，也不由系统未经用户发起便自动新建并执行。

确认同一会议地点文本所对应的真实地点候选，属于消除歧义；改去另一个会场，属于修改核心请求。两者必须区分。

尝试在答复中修改核心请求时，返回“需要新建任务”的明确结果，当前交互不消费、原请求不改变、不推进模型或工具；界面允许用户继续回答原问题或取消旧任务后自行新建。服务端不只靠模型理解自由文本：结构化答复不提供核心字段更新入口，原请求始终作为后续调用和最终校验的权威约束。

偏好补充不得绕过已确认选择、核心请求或业务校验；文本与结构化选择冲突时要求澄清，不静默选择其中一个。如果未来需要原任务内修改核心请求，需另行定义 request_revision、受影响证据失效和重新查询，不属于此次兼容约定。

拒绝某个候选不等于取消整个任务。候选拒绝作为人工结果返回，模型可在剩余预算内继续；显式取消结束任务。用户不能通过回复改写供应商事实、政策原文或豁免时间地点校验。首版不包含预订、支付及审批权限系统。

## 3. 调用方兼容：旧入口不返回等待状态

**C01 — 保留 plan_trip 的现有调用契约。**

现有参数及依赖注入保持可用，包括 provider、store、limits、otel、evaluation_id、tool_mode、policy_path、transport_http。既有调用默认非交互，运行至终态后返回原 run 字典；保留 verdict、draft、termination、计数器及 real 模式证据字段的原有语义。

非交互入口不暴露人工工具，模型越权调用仍走未知工具处理。不能让原 evaluation.py 或 CLI 意外收到 waiting。已知初始化异常和存储异常的外抛边界，不在 HIL 重构中无说明地改成成功结果。

**C02 — 新增显式交互入口，与旧入口共享一个执行内核。**

建议 Python 入口为 start_trip、get_trip、respond_trip、resume_trip、cancel_trip；名称可在实现前调整，语义固定：start/resume 推进到等待或终态，get 只读，respond 原子接收答复后继续，cancel 结束等待任务。运行中取消若要支持，另定义安全边界，不中断正在进行的外部操作。

建议交互返回 envelope：run_id、trace_id、execution_status、state_version、human_request、result。等待时 result 为 null，终态时 result 为与现有 run 兼容的最终结果。计数摘要可以单独提供；不得伪造最终 verdict 或 finished_at 表示等待。

首版 run_id 与现有 trace_id 使用同一稳定值，避免另造无法关联的业务标识；进程段与 HTTP job 可以各有自己的 ID。恢复不生成新的业务 Trace ID。

provider 实例、网络客户端和 transport_http 不序列化。旧入口仍接受实例；交互入口通过受控 provider/tool 工厂和非敏感配置重建。测试工厂必须可在新进程重建，不依赖上个进程的内存对象。

**C03 — HTTP、CLI 与评测分别兼容。**

原 /api/run、/api/jobs、/api/runs 和评测接口默认行为保留。交互接口建议放在 /api/hil/runs 及其下的查询、respond、resume、cancel 路径；具体 URL 不是工具开发的依赖。

交互等待必须落库后返回，释放现有工作线程和 busy 标记。内存 jobs 只表示执行片段，不能作为恢复的唯一依据。服务重启后通过持久化 run_id 查询和回复。读取状态不触发模型或工具。

新增交互页面显示问题、真实候选、等待/过期/取消状态及回复结果。旧任务列表遇到交互任务也须正确展示 execution_status，不能把等待计为执行错误。HTTP 继续执行既有本机、同源、JSON、大小限制与输出转义规则。

旧评测通过 plan_trip 获取终态；新增 HIL 评测通过交互入口显式提交确定性答复。等待不能计为成功或失败样本；不得由评测偷偷替真实用户批准。

## 4. 状态、标识与消息协议

**C04 — 生命周期与业务结论分开。**

execution_status 取 running、waiting_for_human、completed、failed、cancelled、expired。stage 表达下一项工作：model_turn、dispatch、await_human、finalize、done。state_version 随持久化状态变更递增，不能用模型 step 代替。

最终 run.status 仍取业务状态 planned、unavailable、needs_review。正常 finish 即使得出 needs_review，也对应 execution_status=completed、termination=completed；预算和执行错误对应 failed。等待没有最终业务结论。

新增交互终止原因 human_cancelled、human_expired 只出现在交互路径，最终业务兜底沿用 needs_review/execution_failed；界面优先展示具体人工终止原因。不要把它们归为 provider_error。

**C05 — 已产生的动作优先恢复，不能重新询问模型。**

running 且无 pending_call/inflight 标记时才可调用模型；running 且 pending_call 已保存时先完成其分派；waiting 时只返回对应人工请求；终态重复读取/恢复不执行工作。inflight 的不确定崩溃按第 8 节处理。

模型生成的人工请求保留 assistant tool_call，回复严格匹配 tool_call_id。每条回复必须有唯一前驱调用，调用和回复顺序正确，非终态至多一个未完成调用，禁止重复 ID、重复回复、无对应调用的回复。仅比较 ID 集合不能证明消息顺序合法。

区分三个标识：模型 tool_call_id 用于对话配对；真实工具 call_id 标识一次业务工具尝试；interaction_id 标识人工请求。不得因为引入 HIL 就把三者当成同一个 ID。缺少模型调用 ID 的离线 provider 可使用稳定、运行内唯一的合成 ID，不改写真实 provider 已返回的合法 ID。

模型主动调用 request_human_input；程序确认存在阻断执行的具体歧义时，也允许发起人工请求。程序发起的请求记录 origin=guardrail，保存待决事项和可能被阻断的工具调用；回复作为应用侧明确标记的人工上下文提交，不伪造模型 assistant 消息，也不增加 model_calls。两类请求共用消费与持久化机制。

暂停位置是本系统选择的安全边界，不是模型 API 规定“所有暂停只能出现在 assistant 与 tool 之间”。普通工具及其同轮重试内部不设置人工暂停点；模型下一轮之前必须修复待完成的消息协议。

## 5. 工具、证据与人工选择

**C06 — 工具只负责业务调用，不负责等待人。**

七个真实工具的名称、参数、数据依赖和结果语义继续服从父规格。后续工具只接入统一分发边界；不导入实验包、不访问交互数据库、不自行启动模型或人工等待。

工具可变状态以可序列化数据保存，运行时客户端重新构建。必须涵盖 calls、http_requests、seen、history、_transport_attempts，以及 demo 的故障剩余次数和数据重建信息。若后续详情工具新增候选索引，也必须纳入恢复状态或从持久化证据确定性重建。

真实工具结果继续保留 call_id、tool、status、data、warnings、error、evidence_refs，以及适用的 attempt、retry_of、http_requests 和 evidence_error。不强迫 real 结果转换成 demo records。demo 的验证索引继续按工具名；real 按 call_id 保存全部历史。未来统一内部容器时必须保留两个适配视图。

**C07 — 人工选择有独立来源，不伪装成供应商证据。**

交互选项使用稳定 option_id，绑定本次已观察候选及 evidence_refs；显示名称和地址等必要信息。答复绑定 interaction_id、state_version、response_id 以及结构化选择，不依赖“第二个”这类展示顺序字符串。

模型返回的选项也要验证来源。伪造候选、未知 ID 或不属于当前请求的选择不消费交互，不调用外部工具。自由文本作为偏好或澄清保存，不能直接成为坐标、价格、班次证据。

已接受选择进入独立 decisions 数据。工具调用使用相关对象时、最终校验时，都检查与选择一致；仅在 prompt 中提醒不足以兑现选择。后续再次查询同一对象产生新证据时，要保存对象关联，不能靠复用旧 ID 隐藏变化。

等待后的真实数据不视为永久有效：保留查询时间、业务日期和确认来源。日期已失效或地点上下文变化时，不直接复用为当前可行方案；应重新查询或明确结束。各工具的数据新鲜度规则由对应真实工具工单说明，不在 HIL 中统一编造报价有效期。

## 6. 预算、重试与记账

**C08 — 暂停不发放新预算。**

step 表示已启动的模型回合数，与 model_calls 同步计入失败尝试，不以成功消息数推导。模型请求人工计一轮；人工答复、程序触发人工请求及恢复动作本身不计模型调用。human_requests 单独累计；仅成功创建一次交互时增加。工具计数不包含人或 finish。

交互模式 timeout_s 使用累计活跃执行时间：模型/工具耗时、失败耗时、重试等待以及执行段内的校验与编排开销均计入；人工等待、进程离线及未调度等待不计入。使用可控时钟验证，并避免阶段重复计时。非交互入口保持现有连续执行预算语义。

人工等待使用独立 UTC expires_at。建议首版 max_human_requests=3、human_ttl_minutes=1440，均属于本草案建议默认值，不能从实验默认值推定为已批准要求；创建后固定，恢复不延长。运行期限与人工期限是不同概念。

保留 duration_ms 为整次运行的墙上历时，另增 active_duration_ms、waiting_duration_ms，不把旧字段静默改成实验中的活跃耗时。跨进程使用 UTC 时间记录历时，单调时钟只用于当前执行段预算。HIL 性能统计不得把人工等待混入模型耗时比较。

检查点包括模型调用前、响应记账和结构校验后且分派前、每次工具尝试前，以及恢复后执行 pending 动作之前。检查时间和 token；工具尝试另外检查工具额度。step 用尽后不再开模型回合，但本轮已启动的合法 finish 仍可完成，不额外扣一轮。模型返回合法人工请求但用量已越界时，也应预算终止，不能先问人。

业务工具尝试、实际发起的 HTTP 请求、重试分别计数。HIL 不新增隐藏 HTTP 重试。已有 max_tool_calls 不能冒充 max_http_requests；需要 HTTP 上限时新增显式配置并由请求边界执行，具体额度及完整流程配置由工单 #10 验收。

**C09 — 保留两种已有重试行为，不在 HIL 中顺便重写。**

demo：TimeoutError 后同一模型回合内最多再尝试一次；每次尝试检查预算，失败尝试也计工具调用。两次均超时返回既有 tool_timeout 结果，然后由后续模型回合处理。

当前 real 交通：工具将供应商失败包装成错误结果，模型后续再次调用同一规范化查询；一次工具执行只发起该次查询，不暗加同轮自动重试。延续已实现的 attempt/retry_of 关联规则：同一查询失败后再次实际请求才算重试，成功后的刷新、不同查询不能误计。恢复须保存查询历史。

因此 demo 重试不增加模型调用，real 模型重试增加模型调用。tests/test_transport.py 的第三次成功、三个 HTTP 请求、两次 retries 的现有断言必须继续成立。#10 若统一策略，需要显式修订本条及相关断言，不能在工具、模型和请求层同时新增重试。

**C10 — 不完整用量不能伪装成完整。**

provider.chat 开始前记录尝试；响应的有效用量在消息结构校验前累计，即使随后 invalid_response 也不能漏记。provider.chat 异常置 usage_complete=False、cost_usd=None。缺失 token 字段置 usage_complete=False，已知 token 继续累计；不能声称未知部分为零。

cost_usd 一旦为 None，不因恢复、重新读取价格配置或后续成功而变回数字。保留初始计价配置与 provider/model 身份；缓存 token 和其他累计值跨暂停延续。失败耗时通过异常路径同样计入。

## 7. 现有终止分支必须覆盖

以下各项都进入 finalize，再到 done；正常完成只用 completed，其余循环错误通常用 failed。不是每个触发条件都需要独立 stage。所列预算/计数/消息变化应与终止结果一起保存。

1. step 耗尽：termination=budget_exceeded、budget_reason=steps，保留最后完成回合及全部累计值。
2. 时间或 token 耗尽：分别保存 deadline/tokens；覆盖入口、响应后和每次尝试前；已获得响应用量必须留下。
3. provider.chat 异常：记录失败模型尝试、不完整用量和未知费用，再按实际异常类型分类。ValueError/TypeError/KeyError/IndexError 在现有外层捕获为 invalid_response，其他 Exception 为 provider_error；不能仅凭异常来自 provider 就改变分类。
4. 调用个数、名称类型或消息字段错误：invalid_response。未知工具额外增加 guardrail_blocks；名称非字符串按当前主链路不额外增加该值。KeyError 等同样覆盖。
5. finish 校验 ValueError：增加 guardrail_blocks，invalid_response；不得保存未通过校验的成功结论。
6. finish 成功：execution_status=completed、termination=completed，保存原业务 verdict 和 draft；可以是 needs_review。
7. 工具尝试前额度不足：budget_exceeded/tool_calls，不执行该次尝试。
8. demo TimeoutError：按 C09 完成同轮重试；首次超时后的等待可能导致下一次尝试被时间/工具额度拦截。重试本身不是终止状态。
9. 工具 ValueError：增加 guardrail_blocks 后 invalid_response，已发生的工具尝试计数不能丢失。
10. 循环其他 Exception：provider_error，仅保存受控异常类型和整数 http_status，不保存原异常文本。名称沿用旧契约，不代表必然是模型供应商故障。

已知差异需先补充复现再实现：real 路径若意外抛出两次裸 TimeoutError，旧 fallback 缺少 call_id，随后可能因 KeyError 终止。不能将此视为真实工具必须保留的协议；应产生保留调用标识的标准错误结果，作为显式修正另设断言。

初始化的非法请求/模式、provider 配置、工具资源构造失败，以及最终 format_draft/save_run 失败，不全在旧循环 try 内。旧入口保留可观察异常契约。HIL 自身存储/快照错误不能误标 provider_error，更不能返回已成功暂停/完成；恢复读取错误保留原持久化状态并向调用方返回受控错误。

## 8. 事务、重复提交和崩溃边界

**C11 — 对已提交人工暂停保证准确恢复。**

创建交互、增加配额、保存问题与快照、设置 waiting 同一事务提交。消费答复、保存 decisions、追加对应消息、清理人工 pending、复位运行状态同一事务提交。网络调用和人工等待均在写事务之外。

同 response_id、同交互、同规范化答复重放，只返回当前状态，不自行再次驱动模型；同 ID 不同内容或归属返回冲突。不同 response_id 再答已消费交互也冲突。幂等检查先于旧版本拒绝，但不能绕过内容一致性。新的有效答复必须匹配最新 state_version。

取消和过期与回复竞争时只允许一个事务获胜；过期状态可在 resume/respond/cancel 时按 UTC 检查并落库，无需首版后台调度。get 返回有效过期投影但不驱动执行。TTL 边界收到回复应拒绝，不能刷新期限。

仅有短事务不足以避免两个进程同时恢复。需按 run_id 获取持久化执行所有权，并在提交时检查版本/所有权；失去所有权的执行者不得覆盖新状态。不要求并发架构优化，但必须验证双 resume、双 respond 的互斥效果。

**C12 — 对未完成外部调用不宣称 exactly-once。**

调用前持久化 inflight 意图和已预留预算，调用后提交结果；已提交结果恢复时不重放。进程死于“远端已执行、本地未记录”时，不能自动认定没发生、补零或无脑再发请求。

首版建议将不能对账的 inflight 运行终止为 execution_interrupted，业务结果 needs_review/execution_failed；保留意图及可知计数，用量无法确定时明确不完整。这是异常崩溃的受控降级，不限制已提交 WAIT 的正常跨进程恢复。后续若允许只读查询自动重放，须单独定义重放次数、预算预留、证据 ID 和不确定用量，不能沿用实验的隐含重放语义。

终态快照与最终 run 投影应在可协调的提交边界完成；终态保存失败可重试，同一 ID 不产生两个结果。finalize 尚未完成时不返回完成；恢复 finalize 只补持久化，不重新调用模型或工具。

## 9. 持久化与观测

**C13 — 快照自足、版本明确、与公开遥测分离。**

至少保存：schema_version、run/trace ID、execution_status、stage、state_version、step、规范化 request、scenario 和模式、prompt/schema/数据/政策指纹、非敏感 provider 配置和计价配置、全部 limits、活跃计时、完整 messages、pending/inflight、human_requests、decisions、run 累计字段、tool_results、工具可变状态、最终结果或收尾标志、并发执行所有权。

密钥、客户端、完整环境变量不进快照；恢复重新读取主项目受控配置，不读取实验目录。可恢复私有消息与公开 run/trace 分开存放，不通过现有列表或 Trace API 暴露完整对话。保留原异常脱敏与证据处理规则。

优先在现有 SQLite 增加独立命名的交互/快照表，不覆盖原 runs/spans/evaluations 表。实验四也叫 runs 的表结构不能直接复制到旧库。旧历史可读取但无快照不可恢复；不得伪造恢复状态。新版本不认识快照或工具协议不兼容时，受控拒绝恢复，不静默用新 schema 执行旧 pending。

非交互调用保留原 span 树；HIL 使用同一个业务 trace_id，每个执行段开启独立 span 树并记录段关联。保留模型、工具、校验、错误及预算属性；不恢复进程内 Tracer.stack，不把人工等待伪装成一个长模型 span。

## 10. 对工单依赖的约束

这里使用 GitHub 编号，不以 OPEN/CLOSED 推定本地实现状态：#2 对应 [01] 政策，#3 对应 [02] 交通搜索，依次类推。HIL 另立增量工单，不追溯扩大已完成验收范围。

- #2、#3：既有政策来源、独立安装、交通时刻/日期、失败与重试断言作为兼容回归；补充暂停前后真实工具状态测试，不要求重做供应商接入。
- #4 交通详情、#5 住宿：按 C06 保存候选关联、类型、日期、报价和证据；业务工具不用等待 HIL UI 完成，但接主循环前应采用已确定的状态接口。
- #6 地点解析：原“有歧义则保留不确定”的验收继续成立。HIL 增量增加有证据的地点选项与确认后约束；非交互模式不能强制等待用户。
- #7 路线：使用已确定或仍明确标记不确定的地点上下文，保留两个路线证据；有阻断歧义时不能绕过确认宣称确定方案。
- #8 完整方案：仍负责 real 的完整行程校验。当前 real 只能 missing_data 的限制在该工单按父规格演进，HIL 不将这一阶段性限制永久固化，也不引入旧演示预算和晚六点出发规则。
- #9 页面自然语言：仍负责首次输入解析。HIL 负责交互展示与回复入口，避免同一工作重复分配；首次解析后固定绝对时间，恢复时不重新解释“下周六”。
- #10 恢复/预算/最终联调：保留真实 API 整链、HTTP 预算、有界恢复、语义评测和隔离安装责任。C08—C10 的跨暂停含义提前确定，不能最后才重写；修改重试所有者时须同步本约定与既有测试。

建议交付顺序：约定审阅 → 非交互行为等价的可恢复执行内核 → 最小人工交互闭环（demo 加已接入 real 工具的固定外部响应）→ 后续工具按原依赖继续 → 地点确认业务联调 → 完整真实链路验收。不把真实 API 全部接齐设为 HIL 前置条件，也不把 HIL UI 设为所有工具内部实现的前置条件。

## 11. 实现前固定的验收断言

这些是待实现门禁，不是本次测试结果。每项绑定前述约定，避免只证明实验本身可以运行。

1. C01/C03：原 plan_trip、HTTP、CLI、评测收到原终态结构；HIL 关闭时原 schema/prompt 不意外增加人工工具。固定输入对比新旧 verdict、工具行为、适用计数，排除 ID/时钟等非确定字段。
2. C04/C05/C11：进程 A 查工具并提出一次人工问题后落库退出；进程 B 不答复则不调用模型；答复后只新增一条对应消息，从下一轮继续。同次两次暂停均不重置 step、证据或配额。
3. C08/C10：已用 10 秒、总预算 180 秒，人工等待七天且配置 TTL 八天，恢复预算仍为 170 秒；默认 24 小时 TTL 下同样等待则过期。模型异常、非法响应均保留已知尝试/用量及实际活跃耗时。
4. C08/第 7 节：合法 finish 随本轮响应使 token 达上限时为 budget_exceeded/tokens；工具执行次数不增加。到 max_steps 的最后一轮合法 finish 可以正常结束。时间/工具/人工配额分别核对原因。
5. C09：demo 首次超时、第二次成功为两次工具尝试、一轮模型工具决策、一次重试；真实交通三次模型查询前两次失败为三次 HTTP、三个 call_id、两次 retries，跨人工暂停仍关联同一查询历史。
6. C11：同答复重发无新消息或外部调用；同 response_id 换内容冲突；旧 interaction/旧版本不能消费；双进程 resume/答复/取消竞争只有一个有效状态推进。
7. C07：展示有本次证据的地点 A/B，选择 B 后调用路线必须使用 B；伪造 C 被拒绝，选择 A 的文本不能覆盖 B 的结构化答复。程序触发与模型触发交互均测试消息合法性。回复要求更换会议日期时，不消费交互、不修改 request、不新增外部调用，明确要求新建任务。
8. 第 7 节/C12：预算、provider 各类异常、非法消息、finish/工具校验失败覆盖全部列举分支；调用后结果未保存的注入崩溃不盲目重放，finalize 重试不重复工具和最终结果。
9. C06/C13：恢复后原政策证据哈希、交通调用历史和 retry_of 不变；不兼容快照拒绝；旧库/旧 Trace 可读取；公开 API 不包含快照消息或凭证。
10. C03/C13：网页真实等待、回复、刷新及服务重启可继续；Trace 按段关联；既有非交互观测和评测继续可用。单元测试不能替代这条用户可见闭环。
11. C06/第 10 节：无实验目录隔离安装可执行 HIL 固定响应链；真实工具齐备后，按 #10 做真实模型与真实 API 的到最终页面成功验收，离线通过不能替代线上通过。
12. 对 3/4/6/7 的实现分别注入预算重置、跳过预算检查、重复消费、错误地点等变异，断言必须失败；再由独立上下文对抗复查消息顺序、时间和证据关联。测试的预期值不得调用待测实现计算。

## 12. 审阅结论的记录方式

首版核心需求修改范围已确认并同步到第 2 节和第 11 节。入口命名、人工配额/TTL 建议默认值、并发与不确定崩溃策略属于本文提出的技术约定，不标为用户逐条批准。发布时需把有限 HIL 增量与受影响接口同步到父规格/独立工单；未获发布指令前，本地文档不自动变更远程工单。

最初分析轮只核对文档与代码契约。用户随后授权分阶段实现及提交；基线、执行内核、持久化 HIL 与页面的实际验收情况见 [实施记录](hil-implementation-progress.md)。真实工具尚未接齐的业务断言仍由后续工单联调验证。
