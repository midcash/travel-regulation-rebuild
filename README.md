# 差旅助手

当前是**真实工具与测试底座**，旧 Agent/HIL、演示基线和页面已移除，暂不提供产品启动入口。下一步按一张[最小骨架工单](docs/skeleton-issue.md)接通 Pi 会话、SQLite、API/SSE 与新页面，再逐步验收 [B01–B12](docs/product-behavior.md)。不提供预订或付款。

## 安装与检查

需要 Python 3.11+、Node 22、npm。在仓库根目录执行：

```powershell
python -m pip install -e .
npm.cmd ci --prefix verification/pi-step0 --ignore-scripts --no-audit --no-fund
python scripts/check.py
```

统一检查包含真实工具合同与 Pi 离线进程测试，不需要密钥、不调用在线供应商、不产生模型费用。测试替身只在测试/隔离探针使用；它们不证明产品或真实页面端到端已通过。

`travel_agent/` 保留交通、住宿、地点、路线、脱敏与本地 Trace；`verification/pi-step0/` 保留锁定 SDK 1.3.0 的接入探针。当前接口和遗留限制见[工具合同](docs/tool-contracts.md)，验证边界与后续缺口见[开发说明](docs/development.md)。

真实调用配置见 `.env.example`，不要提交密钥；默认测试不加载 `.env`。历史运行记录及旧数据库未迁移、未删除。恢复清理前代码使用本地标签 `codex/pre-skeleton-cleanup-20261018`（提交 `30619f0`）；更早基线在 `legacy-pre-pi`。标签只恢复版本化文件，不能恢复未跟踪数据。
