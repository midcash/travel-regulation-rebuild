# OpenClaw 官方源码参考

- 上游仓库：[github.com/openclaw/openclaw](https://github.com/openclaw/openclaw)
- 本地目录：`source/openclaw/`
- 快照：2026-09-15，浅克隆/稀疏检出，仅用于学习，不参与本实验运行
- 许可证：以上游仓库 `source/openclaw/LICENSE` 为准

建议阅读：

- `docs/agent-runtime-architecture.md`：runtime 如何负责模型发现、工具接线、prompt、session 和 channel delivery
- `docs/concepts/agent.md`、`docs/concepts/agent-loop.md`：Agent 与循环边界
- `docs/gateway/config-tools.md`：工具策略链和 hard-deny
- `docs/gateway/sandboxing.md`、`docs/tools/exec.md`：sandbox、审批和高风险执行
- `src/agents/embedded-agent-runner/`：运行时执行、compaction、失败和 session 相关实现

本目录旁边的 `../production.py` 是教学用的 Python 简化实现，不是上游源码的改写或再发布。
