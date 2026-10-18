# 开发说明

当前仍为旧 Python 实现；[Pi 迁移方案](pi-migration-plan.md) 是下一批实施依据，[product-behavior.md](product-behavior.md) 是目标行为。清理前提交已打 legacy-pre-pi 标签。

## 当前代码

- main.py / start-local.ps1 调用 travel_agent/cli.py；启动脚本仅保留端口，固定 DeepSeek。
- agent.py 的 TripExecution / plan_trip 为旧循环，hil.py / human.py 为旧人工交互，核心需求修改仍要求新任务。
- server.py / web/ 为本机工作台，保留同源、Host 和客户端头校验，无多用户身份系统。
- domain.py 包含请求规范化与旧方案合同，request_parser.py 为有限规则解析。
- real_tools.py 分派真实工具，transport*.py、stays.py、places.py、route.py、supplier_wire.py 和 regulation_policy.py 为供应商/政策适配；现有语义见 [工具合同](tool-contracts.md)。
- telemetry.py 保存 SQLite Trace，可选 OTLP；evaluation.py / evals/ 是待移除的旧演示回归。

主包不依赖实验包。暂留实验目录仅被第 0 步历史回放读取，不是新实现规范。

## 运行与验证

安装启动见 [README](../README.md)。旧 CLI run/eval/scenarios、baseline/demo 和网页控件仍因依赖暂留，将与旧引擎同批删除。run 和 HTTP 旧入口默认 demo，不能作为真实验收。SQLite 默认 artifacts/observability.sqlite3，不删除或改写用户记录。

```powershell
python -B -m unittest discover -s tests -v
node tests/agent_workspace_ui.test.cjs
node tests/hil_ui.test.cjs
node tests/request_ui.test.cjs
python -X utf8 -m unittest discover -s verification/pi-step0 -p test_contracts.py -v
```

使用有效 Python 3.11+。第 0 步合同测试需要先在 verification/pi-step0/ 安装锁定的 Node 依赖。上述检查不需在线模型/供应商；第 0 步 live/probe/replay 会调用真实模型并收费，不能混入默认测试。假模型只用于自动测试。

旧预算仍是模型 10 次、工具 12 次、24,000 token、180 秒活跃时间、单模型 45 秒，HIL 默认 3 次。这些仅是待删除旧代码的回归值，不是新 Pi 要求。Pi 开发控制改为完整计量与熔断（模型30/工具50/累计50万token/活跃10分钟/单模型45秒），项目每日50元，金额unknown时每日500万token；以迁移方案决策记录为准，尚未实施。

回归用固定时钟/日期，页面示例用服务端上海时间加三天。每条新消息捕获上海时间、工具执行再次检查过去日期，是新链路必须实现的行为。历史解释不改写原查询时间。

## 留存与迁移

保留 [第 0 步报告](pi-step0-report.md)。机器结果移到忽略的 artifacts/pi-step0/pi-step0-results.json，报告脚本也写入该处；新克隆可用 git show legacy-pre-pi:docs/pi-step0-results.json 查原始结果。旧归档、HIL 说明、回归基线和前置条件也保存在标签中。

正式接入拆为 M01/M02、M03a 接口与成果区、M04a 桥接回放、M04b API/SSE 与数据收集、M03b 真实三亚及同批删除。M03b 完成前不切换默认入口；B01–B12 按实际依赖并行推进。旧 HIL 仅本地测试数据，不做历史投影，不修改、不删除旧表。replay.py 与最小脱敏夹具迁至 tests/e2e/ 后删除 experiments/。预算含缓存命中输入，核对来源见 [预算审计](pi-budget-audit.md)。正式预算由 B01–B12 后的预算优化工单设定，迁移阶段不设预算门禁。
