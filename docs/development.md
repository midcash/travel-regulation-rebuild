# 开发与测试

当前仅保留真实工具、本地 Trace 与 Pi 接入探针；没有产品网页、HTTP 服务或 CLI。骨架交付边界见 [下一张工单](skeleton-issue.md)，业务验收以 [B01–B12](product-behavior.md) 为准。清理后的工具合同仍有局部旧限制，见 [tool-contracts.md](tool-contracts.md)，不能把这些限制作为新业务要求。

## 一条检查命令

安装步骤见 [README](../README.md)。仓库根目录运行 `python scripts/check.py`，失败返回非零退出码；CI 使用相同命令。它依次执行：

```powershell
python -B -X utf8 -m unittest discover -s tests -v
python -B -X utf8 -m unittest discover -s verification/pi-step0 -p test_portable.py -v
```

`tests/test_tools_*.py` 直接调用真实适配器，以合成 HTTP 替代供应商；覆盖交通/详情、酒店/详情、地点与路线、证据、金额精度、失败、重试、脱敏及本地 Trace。没有复用旧 Agent loop、HIL 或私有历史夹具。`tests/suppliers.py` 是合成线协议数据，不是历史线上记录。

Pi portable 测试经 Python→实际 Node 子进程→锁定 Pi SDK→SQLite，覆盖流式消息、工具提交幂等、取消/强杀、压缩恢复、未知用量和资源隔离。验证库均在系统临时目录；默认检查不读 `.env`、本机历史运行数据或在线供应商。SDK 的 faux token 不代表真实账单。

## 可控测试边界

`RealTools` 可注入 `request`、`current_date`、`evidence_dir`、`transport_http`，`execute` 可指定超时。白盒检查关注解析、状态、金额与错误分支；工具黑盒检查只观察参数、返回值、HTTP 次数和证据。供应商连接使用 `httpx.MockTransport`，临时目录和 SQLite 自动关闭后清理。不要把真实工具本身 mock 掉。

`current_date` 目前是调用者提供的日期快照，尚不能证明跨午夜重新检查；工具历史也只是运行内快照，尚无产品会话数据库或跨会话授权边界。正式骨架需要可注入时钟，并在每次供应商执行前重读当前日期。

## 端到端还缺什么

- 正式 Python 会话服务、按会话隔离的 Node 运行时与持久化事务；进程探针不是这些模块的实现。
- 正式 HTTP/SSE 的启动/关闭、临时端口/数据库、模型边界替身、失败与重连测试。
- 新页面及浏览器测试依赖、安装步骤、可访问定位、服务启停夹具和失败截图/Trace；现阶段不保留空壳浏览器测试。
- 独立真实验收命令：当次未来日期、真实 DeepSeek/供应商、页面输入到成果区及来源证据；真实凭证不足或服务不可用须写未验收，不能拿离线通过代替。

上述能力应随 [骨架工单](skeleton-issue.md)交付；完整业务仍需逐项核对 B01–B12。测试权限可以让 Agent 执行检查，不能替代缺失的运行链路与验收依据。

## 真实模型探针

仅在核对 SDK 接入、计量或故障时按 [探针说明](../verification/pi-step0/README.md)显式执行。`probe/live` 加载现有 `.env` 并收费，`live` 需 `python -m pip install -e ".[otel]"`；只调用模型和合成工具，不调用真实差旅供应商，也不证明产品真实端到端。正式运行安全和用量上限见 [Pi 决策](pi-migration-plan.md)。

## 清理与恢复

清理前提交为 `30619f0dc5debbb64a85a6c21c7d4795a4baaecf`，本地标签 `codex/pre-skeleton-cleanup-20261018`。旧演示/评测、HIL、页面、实验源码及通用技能副本均从工作树移除；不读取、不删除忽略的运行数据或依赖。不做旧 HIL 历史投影，不修改旧表。更早测量见 [精简后的第 0 步记录](pi-step0-report.md)，完整旧材料通过 Git 查询。
