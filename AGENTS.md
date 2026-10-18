# 开发入口

- 开始开发先读 `README.md`；运行、测试和当前缺口见 `docs/development.md`。
- 涉及业务行为读 `docs/product-behavior.md`；涉及领域术语读 `CONTEXT.md`、`docs/agents/domain.md`。
- 实现 Pi 会话骨架读 `docs/skeleton-issue.md`、`docs/pi-migration-plan.md`。仅核查历史接入结论时读 `docs/pi-step0-report.md`。
- 修改真实工具读 `docs/tool-contracts.md`；保留输入校验、证据归属、未知金额和脱敏。
- 操作 GitHub 工单读 `docs/agents/issue-tracker.md`、`docs/agents/triage-labels.md`。

# 验证与数据边界

- 默认检查：`python scripts/check.py`；安装步骤见 README。缺依赖应报错，不能静默跳过。
- 测试使用临时库/目录、固定时钟和模型/供应商边界替身；工具合同与 Pi 探针不能代替正式服务、浏览器或真实端到端验收。
- `probe/live` 调用真实模型并收费，显式执行并遵守 Pi 方案的日累计保护；默认检查不运行它们。
- 用户密钥、SQLite 和历史运行数据保留原样；不为清理源码修改或删除旧表/记录。
- `experiments/` 中可能仍有本机未跟踪旧数据，禁止阅读；`artifacts/`、`.tmp/` 只在用户指定的诊断或验收范围内访问。
- `.git/` 只通过 Git 命令查询；`.venv/`、`node_modules/`、`**/node_modules/`、`build/`、`dist/` 禁止通读，安装、执行测试及核对锁定接口所需的精确访问除外。
