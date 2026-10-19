# 差旅助手

当前仓库提供真实差旅工具适配器、测试和 Pi SDK 接入探针，**尚未形成可供用户使用的产品 MVP**：目前没有正式会话服务、产品 HTTP API、页面或 CLI。不会创建订单或付款。

## 业务依据

- [CONTEXT.md](CONTEXT.md)：领域术语与语义。
- [已确认的业务行为与验收场景](docs/product-behavior.md)：B01–B12。它们是目标行为，不代表当前已全部实现。

## 环境与默认检查

需要 Python 3.11+、Node 22 和 npm。在 Windows PowerShell 的仓库根目录执行：

```powershell
python -m pip install -e .
npm.cmd ci --prefix verification/pi-step0 --ignore-scripts --no-audit --no-fund
python scripts/check.py
```

默认检查运行工具测试与 Pi 离线进程测试，不需要真实密钥，不调用在线供应商，也不产生真实模型费用。测试通过不等于正式产品或真实端到端验收通过。

## 真实模型探针

探针说明与命令见 [verification/pi-step0/README.md](verification/pi-step0/README.md)。真实模型调用可能产生费用，必须显式执行并先估算成本；该探针使用合成工具，不调用真实差旅供应商，也不代表产品验收通过。

## 密钥与运行数据

真实调用配置示例见 `.env.example`。不要提交密钥；默认检查不加载 `.env`。本机历史运行记录和旧数据库不属于代码清理范围，不要因文档或源码调整而迁移、覆盖或删除。
