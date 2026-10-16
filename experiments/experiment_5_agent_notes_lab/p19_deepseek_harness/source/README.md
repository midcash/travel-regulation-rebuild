# DeepSeek Harness 官方源码参考

- 上游仓库：[github.com/deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness)
- 本地目录：`source/deepseek-harness/`
- 快照：2026-09-15，浅克隆/稀疏检出，仅用于学习，不参与本实验运行
- 许可证和安全说明：以上游仓库 `source/deepseek-harness/LICENSE`（若存在）及 `SAFETY.md` 为准

建议阅读：

- `docs/architecture.md`：Cordis runtime、插件、服务和事件模型
- `packages/`：core、session、agent、tools、interaction 等插件化边界
- `python/sdk/src/deepseek_harness/`：对外 SDK、模型和错误定义
- `SAFETY.md`：开发预览阶段的安全边界和使用提醒

本目录旁边的 `../production.py` 是可运行的 Python 教学骨架，重点演示插件依赖、生命周期和事件事实源。
