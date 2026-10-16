# Hermes Agent 官方源码参考

- 上游仓库：[github.com/NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)
- 本地目录：`source/hermes-agent/`
- 快照：2026-09-15，浅克隆/稀疏检出，仅用于学习，不参与本实验运行
- 许可证：以上游仓库 `source/hermes-agent/LICENSE` 为准

建议阅读：

- `run_agent.py`：主循环、模型调用、错误恢复、memory provider、prompt builder 和后台 review 的装配位置；文件中还保留了上游模块的兼容导入表
- `hermes_state_gateway.py`、`hermes_state_sessions.py`、`hermes_state_messages.py`：会话、消息和状态网关
- `hermes_state_wal.py`、`hermes_state_dbfile.py`：WAL/数据库持久化和恢复边界
- `hermes_state_search.py`、`hermes_state_compression.py`、`trajectory_compressor.py`：搜索、压缩和轨迹整理
- `toolsets.py`、`model_tools.py`、`mcp_serve.py`：工具集、模型工具和 MCP 接口

本目录旁边的 `../production.py` 只提炼“提案—证据—审核—发布”这条学习主线，不替代上游实现。
