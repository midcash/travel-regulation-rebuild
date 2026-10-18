# 差旅 Agent

面向差旅咨询、查询、方案生成与调整。当前生产入口仍为 Python 旧循环与 HIL，网页为左侧历史、中间交互、右侧成果。Pi 第 0 步已验证，正式运行链路尚未切换。目标行为见 [B01–B12](docs/product-behavior.md)，实施顺序和删除边界见 [Pi 迁移方案](docs/pi-migration-plan.md)。不提供预订或付款。

## 安装与启动

需要 Python 3.11+。项目根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
# 可选 OTLP
.\.venv\Scripts\python.exe -m pip install -e ".[otel]"
.\.venv\Scripts\python.exe main.py
# 或指定端口
.\start-local.ps1 -Port 8766
```

默认访问 `http://127.0.0.1:8766`，启动脚本固定使用 DeepSeek，不再提供 baseline/演示参数。已有 `.venv` 若指向不存在的解释器，应重建；PyCharm 使用有效解释器运行 `main.py`。

没有 `.env` 时参考 `.env.example` 配置；已有文件保留原配置。模型使用 `DEEPSEEK_API_KEY`，交通与酒店使用 `TUNIU_API_KEY`，地点与路线使用 `AMAP_API_KEY`。环境变量优先，密钥不提交。Pi 正式接入后需要 Node；第 0 步在 `verification/pi-step0/` 精确锁定 SDK，当前不被生产入口调用。

## 当前限制

旧程序仍有固定场景、规则解析、前一晚抵达和一晚住宿等限制；不具备 B01–B12 描述的通用持续会话能力。网页暂存旧演示与评测控件，缺少凭证时仍可能回落旧演示；它们将在真实链路切换批次一并删除。最终产品要求模型凭证，假模型只用于自动测试。

`experiments/`、`evals/` 和 `travel_agent/data/scenarios.json` 因现有回放、运行入口或测试依赖暂留，具体引用和第二批删除条件见迁移方案。它们不是新业务规范。

## 开发

[开发说明](docs/development.md) 记录当前入口与验证命令；[第 0 步报告](docs/pi-step0-report.md) 仅证明协议和运行前提，不代表真实供应商到页面验收。旧实现和清理时删除的资料由 `legacy-pre-pi` 标签保留。迁移验收由 [#11](https://github.com/midcash/travel-regulation-rebuild/issues/11) 及其子工单跟踪。
