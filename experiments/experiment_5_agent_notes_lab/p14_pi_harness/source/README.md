# pi-mono 官方源码参考

- 上游仓库：[github.com/badlogic/pi-mono](https://github.com/badlogic/pi-mono)
- 本地目录：`source/pi-mono/`
- 快照：2026-09-15，浅克隆/稀疏检出，仅用于学习，不参与本实验运行
- 许可证：以上游仓库 `source/pi-mono/LICENSE` 为准

建议阅读：

- `packages/agent/`：Agent 核心、session、事件和 compaction 的包边界
- `packages/coding-agent/src/core/system-prompt.ts`：prompt 组装
- `packages/agent/src/harness/session/`：会话树、分支和持久化
- `packages/agent/src/harness/compaction/`：Agent 层压缩
- `packages/coding-agent/src/core/compaction/`：coding-agent 层上下文压缩和恢复

本目录旁边的 `../production.py` 使用标准库复刻“append-only + parentId + lineage”形状，便于调试。
