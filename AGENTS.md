# 必读文档

- 开始开发前：`README.md`、`CONTEXT.md`、`docs/development.md`。
- 涉及业务行为：`docs/product-behavior.md`。
- 涉及 Pi 迁移：`docs/pi-migration-plan.md`；核查第 0 步结论时读 `docs/pi-step0-report.md`。
- 操作工单：`docs/agents/issue-tracker.md`、`docs/agents/triage-labels.md`。
- 修改领域术语：`docs/agents/domain.md`。

# 禁止阅读的目录

- `experiments/`：旧实验，仅允许现有回放测试按既定路径读取夹具。
- `artifacts/`、`.tmp/`：本机运行数据，仅在用户指定的诊断或验收范围内访问。
- `.git/`：使用 Git 命令查询版本。
- `.venv/`、`node_modules/`、`**/node_modules/`、`build/`、`dist/`：禁止通读；执行测试、安装依赖及核对锁定依赖接口所需的精确文件访问除外。
