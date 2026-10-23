# 浏览器航班基线验收工具

白盒测试、HTTP 黑盒测试和浏览器离线夹具集中在本目录，不读取历史运行数据。

- `test_baseline.py`：真实 Node/Pi 1.3.0，合成模型与合成供应商；覆盖同会话追问、纯查询、固定上海时钟、结果证据、无候选、失败、取消、并发、超时及 Worker 崩溃/回收。日期测试验证时钟注入和合成模型生成的工具参数，不能代替真实模型理解能力验收。
- `fixtures.py`：完全合成的响应，`OFFLINE-101` 不是真实航班。
- `serve_offline.py`：Agent/用户共用的浏览器离线夹具，固定时钟为上海时间 2026-10-19 10:00，不调用在线服务；供应商证据只写系统临时目录，退出后清理。仅该夹具提供只读 `/__verification__/counts`，累计本进程实际观察到的 Pi `model_start` 和合成供应商请求数；正式产品没有此接口。
- `test_offline_fixture.py`：核对上述计数随真实 Node/Pi 活动变化，读取页面/会话不增加调用，新会话不抹掉累计数，正式产品不暴露夹具接口。
- `live_smoke.py`：显式付费的真实两轮验收，复用 `.env`，正常 3 次模型请求、1 次真实航班查询；每轮最多 4 次模型请求、每次最多 2048 输出 token，不自动重试。运行前核对价格、估算费用和确认调用范围。它验证服务运行时，浏览器交互仍需单独检查。

仓库根目录安装依赖后执行：

```powershell
python scripts/check.py
python -B -X utf8 -m unittest discover -s verification/baseline -v
python -B -X utf8 verification/baseline/serve_offline.py
```

离线浏览器地址：[http://127.0.0.1:8766](http://127.0.0.1:8766)。按序输入“我要去三亚。”、“杭州，2026 年 11 月 20 日。”，应先追问、后展示合成航班且价格未知。刷新后仍保留结果。检查 Enter 发送、Shift+Enter 换行、桌面与手机宽度。运行中取消和并发冲突有自动化测试。

若 8766 已占用或被 Windows 保留，用 `python -B -X utf8 verification/baseline/serve_offline.py --port 18766` 选择可用端口；以下页面和计数地址同步替换端口。不要停止或重置不属于本次验收的服务。

## Agent 浏览器验收

从仓库根目录运行上面的 `serve_offline.py` 命令，使用 Codex + Ego Lite 操作实际页面。该夹具只识别 [fixtures.py](fixtures.py) 中的固定输入/响应，不能用于评价任意自然语言理解。不要直接用正式服务代替离线夹具。

1. 打开页面，在同源读取 [只读计数](http://127.0.0.1:8766/__verification__/counts) 并记下当前会话与累计数。已有服务先核实用途和状态，不清空未知会话；可由本次验收启动独立夹具进程。
2. 在输入框输入“我要去三亚。”，用 Enter 发送。应询问出发城市和日期、没有航班卡片；从空进程开始时计数为模型 1、供应商 0。
3. 输入“杭州，2026 年 11 月 20 日。”并发送。应展示杭州→三亚、该日期、`OFFLINE-101`、合成航司、10:00→13:00、价格未知及证据。累计数应为模型 3、供应商 1，不能用固定答复的出现代替对卡片事实的核对。
4. 记下 `/api/session` 的会话 ID、消息与结果及调用数，刷新页面，等待结果恢复及 SSE 连接。核对会话/消息/卡片保留，两个调用数均不增长。页面恢复只覆盖进程内刷新，不等于服务重启恢复。
5. 用 Shift+Enter 检查换行不提交，用桌面和手机视口检查卡片、输入和按钮可用。输入“模型失败”可验证错误提示与此前可靠卡片保留；新会话后应能继续使用。取消、并发、超时可结合 `test_baseline.py` 的实际断言核对，不能声称已在浏览器操作过未执行的场景。

计数跨显式新会话继续累计，服务重启后归零；始终比较本次操作前后的差值。接口不返回提示词、供应商原文或凭证，也不能用于真实调用计费。必要时用 Ego Lite 的网络/控制台观察和截图定位问题，不修改页面 DOM 来制造验收结果。

记录本次分支/提交、环境、离线或真实模式、输入、预期与观察、调用数及必要截图/日志位置。发现问题先修复复验，再按 [AGENTS.md](../../AGENTS.md#agent-验收与交付) 交付。证据保存在本次授权的本机验收目录，不提交本机记录；缺环境或未运行的必要项目如实列出，不能计为通过。

## CI 与验证层次

[默认检查](../../scripts/check.py) 顺序运行 `tests/`、Pi `test_portable.py`、本目录 `test*.py`，缺依赖/非零退出都会失败。本目录的 `web_review_checks.mjs` 使用简化 DOM 检查渲染逻辑，属于局部检查。

[Windows CI](../../.github/workflows/ci.yml) 对 PR 和 main 的 push 触发，安装 Python 3.11、Node 22.19.0、两个 npm 锁文件及 Python 包后运行相同检查；没有按分支名跳过测试的条件。CI 不运行 Ego Lite 或付费链路。核对运行对应的提交、步骤和退出结果；没有实际运行时写明“未运行”，不得把 skipped 或部署成功写成测试通过。CI 不能替代 Agent 的浏览器验收和用户验收。

## B01–B12 证据索引

以下仅索引 [目标业务行为](../../docs/product-behavior.md) 与当前代码/测试，不复写业务定义，也不是产品验收通过清单。合成模型固定产生响应；它证明工程链路，不能证明真实模型会正确理解请求。底层工具测试也不意味着该能力已接入页面。

- [B01](../../docs/product-behavior.md#b01-按当前查询交付)：[运行时](../../travel_agent/runtime.py) 仅开放航班；[Baseline.test_two_turn_query_refresh_date_facts_and_empty_failure](test_baseline.py) 覆盖合成查询/无候选。[ToolsContract.test_flight_search_preserves_facts_unknown_price_and_evidence](../../tests/test_tools_contract.py) 核对底层事实。酒店未接入产品，航司条件与真实模型行为未据此验收。
- [B02](../../docs/product-behavior.md#b02-只追问当前必要信息)：同一 `Baseline.test_two_turn_query_refresh_date_facts_and_empty_failure` 验证固定追问且首轮零供应商请求；`test_blank_input_and_server_validation` 验证非法输入不发供应商请求。运行时提示表达当前范围；任意表达下是否恰当追问仍需真实模型验证。
- [B03](../../docs/product-behavior.md#b03-有理由的推荐)：当前只有事实卡片和答复；尚无完整推荐权衡、选定项优先的产品验收证据。
- [B04](../../docs/product-behavior.md#b04-在同一会话转向)：当前没有酒店/高铁产品路径，尚无跨工具转向验收证据。
- [B05](../../docs/product-behavior.md#b05-明确修改与条件冲突)：当前没有完整选定项/冲突处理实现与产品验收；[Pi 探针](../pi-step0/test_portable.py) 的条件版本注入只证明协议，不证明业务修改语义。
- [B06](../../docs/product-behavior.md#b06-比较不等于采纳)：当前没有完整比较/采纳状态与产品验收；不能把探针的选定项注入当作已实现。
- [B07](../../docs/product-behavior.md#b07-需要的行程形态)：[ToolsContract.test_multi_night_stay_and_detail_keep_dates_unknown_basis_and_raw_rates](../../tests/test_tools_contract.py) 只覆盖底层多晚住宿参数与事实；去返程组合、当天往返和完整方案未接入当前产品。
- [B08](../../docs/product-behavior.md#b08-刷新与重启后继续)：`Baseline.test_two_turn_query_refresh_date_facts_and_empty_failure` 与上述浏览器流程覆盖进程内刷新；[test_offline_fixture.py](test_offline_fixture.py) 核对读取不新增调用。服务重启恢复尚未实现；Pi 临时 SQLite 恢复与底层工具 snapshot 均不构成产品会话恢复。
- [B09](../../docs/product-behavior.md#b09-历史依据与当前核验)：[ToolsContract.test_retry_history_tracks_actual_query_and_successful_refresh](../../tests/test_tools_contract.py) 覆盖底层历史与刷新区分，[Provenance](test_review_regressions.py) 核对查询尝试/观察时间。隔日业务回顾、选定项不被刷新替换尚未完整实现/验收。
- [B10](../../docs/product-behavior.md#b10-制度与预算分开)：当前无制度/预算完整处理与验收证据；[工具注册表](../../travel_agent/real_tools.py) 不提供默认制度查询，不等于已满足完整目标。
- [B11](../../docs/product-behavior.md#b11-部分失败仍交付可靠成果)：`Baseline.test_local_failures_are_distinct_from_actual_supplier_failure`、[Provenance](test_review_regressions.py) 与 [供应商工具测试](../../tests/test_tools_contract.py) 覆盖错误分类、来源和未知费用；跨航班/酒店的部分交付尚无产品路径与验收证据。
- [B12](../../docs/product-behavior.md#b12-业务范围与选定的含义)：运行时提示声明范围，[Node Worker](../../travel_agent/node/worker.mjs) 仅开放航班工具，`Baseline.test_blank_input_and_server_validation` 拒绝其他工具。当前没有订单/付款工具；真实模型的范围回答及选定项业务语义未据此验收。

真实模型/供应商/浏览器证据需在各次授权验收后另行报告，本索引不把历史报告自动继承为当前版本通过。

## 真实链路与生命周期

真实产品运行：`python -B -X utf8 -m travel_agent`，默认仅监听 `127.0.0.1:8765`。手动完成同样两轮，再刷新核对；也可另开新会话查询“杭州明天去三亚的航班”，核对上海日期与结果日期一致。真实证据写入忽略目录 `artifacts/baseline/<session-id>/`，不覆盖已有数据。

执行真实查询前先估算费用；未知供应商费用不能写零。凭证只从环境或现有 `.env` 读取，页面和测试输出不显示密钥。此目录不保存真实运行记录。

取消优先执行 Pi `abort()`；已发出的 Python HTTP 请求在供应商超时内收尾，不再回传给模型。收尾期间会话保持忙碌。Worker 在正常完成和可恢复错误后空闲复用，服务关闭时回收；Worker 异常丢失上下文时保留页面成果并要求显式开始新会话。服务重启恢复及 B08 完整验收尚未实现。
