# 差旅 Agent 开发项目

当前实现是 Python 差旅规划程序：自写执行循环、真实供应商工具、可暂停恢复的人工交互（HIL），以及左侧任务记录、中间输入与交互、右侧成果的网页工作台。数据与 Trace 保存到 SQLite，可选导出 OTLP。不提供预订或付款。

已确定后续采用 Pi，并在差旅范围内支持持续交互和修改既有选择；已确认的目标与验收场景见[业务行为约定](docs/product-behavior.md)。Pi 尚未接入，当前任务也还不能作为可任意修改需求的持续会话使用。文档清理不代表迁移或业务验收已完成。

## 安装与启动

需要可用的 Python 3.11+。新环境在项目根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
# 需要 OTLP 导出时安装可选依赖
.\.venv\Scripts\python.exe -m pip install -e ".[otel]"
```

没有 `.env` 时，从 `.env.example` 复制后填写配置；已有文件请直接编辑，保留现有配置。DeepSeek 使用 `DEEPSEEK_API_KEY`；途牛交通和酒店使用 `TUNIU_API_KEY`；高德地点和路线使用 `AMAP_API_KEY`。环境变量优先于 `.env`，密钥不提交。

```powershell
# 默认以 DeepSeek 启动，监听 http://127.0.0.1:8766
.\.venv\Scripts\python.exe main.py
# 无模型凭证时，以规则基线查看演示模式
.\start-local.ps1 -Provider baseline
```

PyCharm 使用有效的项目解释器运行 `main.py`，无需参数。启动脚本依赖本机 `.venv`；若启动器指向失效的 Python，需先修复或重建环境，仓库不保证已有虚拟环境可直接使用。

网页默认自然语言输入，并勾选“需要时询问我”；有可用模型配置时默认真实工具，否则切换演示模式。结构化会议表单和固定演示场景仍可选择。API、Python `plan_trip` 和 CLI 的默认行为另见[开发说明](docs/development.md)，不要从网页默认值推断它们的默认值。

## 当前能力与限制

- 真实模式接入七项只读能力：政策快照、交通搜索与详情、住宿搜索与详情、地点解析、驾车路线。完整方案检查交通、住宿、两段路线、证据与到会时间；未知费用不填零。详见[工具合同](docs/tool-contracts.md)。
- 演示模式使用杭州到上海的固定数据和独立演示规则。演示报价、预算和固定回归结果不代表真实供应商能力或生产政策。
- HIL 支持需求确认、候选选择、偏好补充、取消和同机持久化恢复。修改核心需求目前仍要求新建任务；这是待替换的现有限制。详见[人工交互说明](docs/hil-usage.md)。
- 自然语言入口是有限规则解析，不是通用语义理解。目前不支持“三天后”；页面快捷示例已按服务端上海当前时间生成明确日期，并补充目的地。解析器固定前一日抵达，不能正确表达任意住宿要求。

## 开发与验证

代码入口、验证命令、预算口径及已知回归差异见[开发说明](docs/development.md)。既有记录保留在[历史验证归档](docs/archive/README.md)，只证明其注明版本和范围内的检查，不作为当前全部测试通过或完整真实链路验收通过的声明。

`experiments/` 是独立实验和学习材料，不是生产实现规范。仅在核查工具来源或复现实验时阅读各实验说明；主包运行不依赖实验目录。

Pi 迁移前的能力与回放验证位于 `verification/pi-step0/`，不改变当前生产入口；见[验证前约定](docs/pi-step0-criteria.md)、[回归基线](docs/regression-baseline.md)和[验证报告](docs/pi-step0-report.md)。
