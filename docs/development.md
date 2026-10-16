# 当前代码开发说明

本文说明当前实现及已知限制。新增需求以用户确认内容为准；历史规格不能自动升级为当前要求，代码现状也不等于正确性保证。

## 代码入口

- `main.py` / `start-local.ps1` / `travel_agent/cli.py`：启动及 run、eval、trace 命令。
- `agent.py`：`TripExecution` 执行内核、预算与快照；`plan_trip` 同步运行到终态，默认 `tool_mode="demo"`。
- `tools.py` / `prompts/planner.txt`：演示工具和规则；`real_tools.py` / `prompts/real_planner.txt`：真实工具分派、状态和协议。
- `transport.py`、`transport_detail.py`、`stays.py`、`places.py`、`route.py`、`supplier_wire.py`：供应商适配与证据；`regulation_policy.py`：随包政策读取。
- `domain.py`：请求规范化、结果校验、草稿；`request_parser.py`：有限自然语言规则解析。
- `hil.py` / `human.py`：人工请求、候选约束、幂等与恢复；接口见[人工交互说明](hil-usage.md)。
- `server.py` / `web/`：本机 HTTP 服务和工作台；`telemetry.py`：SQLite 与可选 OTLP；`evaluation.py` / `evals/`：固定演示回归。

主包由 `pyproject.toml` 发布，包含 data、prompts、policies 和 web 资源，不包含 experiments。真实工具使用主项目配置，不导入实验包或读取实验配置与历史结果。

## 运行与配置

安装与网页启动见[README](../README.md)。服务仅绑定回环地址；写请求校验 Host、同源、客户端头及 JSON，当前不提供多用户权限系统。任务执行由单 worker 处理，等待人时释放 worker。

CLI `run` 默认 demo，真实查询需显式指定 `--tool-mode real`；HTTP `/api/run` 与 HIL 创建未提供 tool_mode 时也默认 demo。网页有模型配置时默认 real，API/网页真实模式拒绝 baseline。CLI 的 provider 默认 DeepSeek，Python `plan_trip` 由调用者传入 provider；调用者应选择适配真实工具的 provider，不应将规则基线用于真实链路验收。

```powershell
.\.venv\Scripts\python.exe -X utf8 -m travel_agent run --provider baseline
.\.venv\Scripts\python.exe -X utf8 -m travel_agent run --provider deepseek --tool-mode real --request-file request.json
.\.venv\Scripts\python.exe -X utf8 -m travel_agent trace TRACE_ID
```

`request.json` 是调用者准备的 UTF-8 结构化请求，字段由 `domain.normalize_request` 定义，使用未来日期。CLI 结构化调用不等同于网页输入校验：自然语言和自定义请求经 `server.prepare_scenario` 捕获上海时间并拦截过期日期；固定 scenario 入口不经过同一日期检查。恢复保留绝对时间，不重新解析相对日期。

观测数据库默认 `artifacts/observability.sqlite3`，CLI 评测报告默认 `artifacts/latest-evaluation.json`。未知模型费用显示未计价；真实行程缺少完整费用时不填零。配置项以 `.env.example` 和 `settings.py` 为准，不记录密钥或完整环境。

## 验证入口

在有效解释器及已安装依赖下运行：

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v
node tests/agent_workspace_ui.test.cjs
node tests/hil_ui.test.cjs
node tests/request_ui.test.cjs
.\.venv\Scripts\python.exe -m travel_agent eval --provider baseline
```

Python 测试包括本地 HTTP 和可选 OTLP，不需要真实供应商调用；前端测试需 Node。真实工具合同测试在外部响应边界注入固定数据。`eval` 仍只驱动固定 demo 案例，不支持把其通过率当成真实工具或持续会话的质量证明。真实模型 demo 评测需显式使用 `eval --provider deepseek`，会产生模型调用费用。

完整真实链路需另行验证模型、供应商、最终校验和页面展示；候选及报价不能固定为历史商品。历史记录与验证范围见[归档](archive/README.md)，不继承为当前通过声明。

## 预算与回归基线

2026-10-16 准备 Pi 第 0 步时，现场 HEAD 为 `0b7364f`，工作区干净。此前文档清理记录的未提交修改已经被该提交保存；本轮明确撤回未验证的上限扩大，恢复旧演示的预算，Pi 预算由独立实测决定：

- `Limits`：10 轮、12 次工具、24,000 token。总活跃执行上限仍为 180 秒，单次模型上限 45 秒。
- HIL：默认 3 次人工请求，默认等待期限仍为 1,440 分钟。HIL 预算跨暂停累计。
- 回归场景固定为 2026-09-12，oracle 保持独立；HTTP 集成测试注入固定上海时钟，不依赖实际今天。页面结构化时间和快捷示例由 `/api/config` 的服务端上海时间加三天生成，使用当前解析器支持的明确日期。

`evals/gates.json` 的工具门禁仍为 12 次；它是演示评测标准，与运行时上限不是同一设置。HTTP 请求数有记录，但没有独立的 HTTP 请求数量上限。demo 超时同轮最多重试一次，real 由模型后续重复调用，二者计数不同；不得暗加多层重试。

基线修复验证见 [回归基线](regression-baseline.md)。这些预算仍是旧演示设置，不是 Pi 产品预算。

## 已知限制与后续方向

自然语言解析目前支持部分周日期、明确日期和有限字段；不支持“三天后”，页面快捷示例已改为明确日期。目的地需明确给出，或由已覆盖的“三亚湾”会场前缀识别；解析器固定前一日抵达，不能正确表达任意住宿要求。这些是待处理缺陷，不是新业务验收要求。

已选择 Pi 会话 SDK 加 Python 管理的 Node 子进程，SQLite 为唯一恢复依据，优先内存 Pi 会话。当前生产入口未切换；第 0 步依赖和验证位于 `verification/pi-step0/`，不进入主包。具体约定见 [验证前约定](pi-step0-criteria.md)，结果见 [验证报告](pi-step0-report.md)。

旧 `.venv` 仍失效；本轮使用已有 Python 3.11.11 验证。真实供应商到页面的完整新链路验收仍是后续工单，不能用第 0 步回放代替。
