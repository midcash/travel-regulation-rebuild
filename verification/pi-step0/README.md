# Pi 第 0 步验证

这是隔离验证工具，不是新生产运行时。依赖精确锁定 Pi SDK 1.3.0。先阅读 [通过/失败条件](../../docs/pi-step0-criteria.md) 和 [最终报告](../../docs/pi-step0-report.md)。

```powershell
npm.cmd ci --ignore-scripts --no-audit --no-fund
python -X utf8 -m unittest discover -s verification/pi-step0 -p test_contracts.py -v
python -X utf8 verification/pi-step0/run.py offline
python -X utf8 verification/pi-step0/run.py probe
python -X utf8 verification/pi-step0/run.py live
python -X utf8 verification/pi-step0/run.py replay
```

`npm ci` 在本目录运行；Python 命令在仓库根目录运行。`offline` 使用 Pi 官方 faux provider，不需要密钥，也没有真实账单。`probe/live/replay` 加载仓库现有 `.env` 中的 DeepSeek 凭证，不输出密钥，会产生模型费用；型号固定为本轮账号确认可用的 `deepseek-flash`，推理强度 low。不调用真实差旅供应商，不自动降级到模拟模型。

Node 在空临时目录启动，HOME、USERPROFILE、APPDATA、LOCALAPPDATA、PI_CODING_AGENT_DIR 都被重定向，PI_OFFLINE 禁止目录刷新。SDK 资源加载器、设置、凭证和模型目录均为内存实现。文件探针在动态导入 SDK 前安装；这是回归检测，不是抵御任意恶意代码的系统沙箱。

工具通过双向 NDJSON 交给 Python。工具调用 ID 由 Pi 会话 ID 和模型工具调用 ID 组成，避免子进程重启后计数器碰撞。Journal 先提交结果再回传；提交前中断为 unknown，新查询使用新 ID。只有隔离验证库使用 `pi0_` 表，生产数据库未改动。

`live` 通过 Python Trace 和本地 OTLP 接收器验证全部模型请求（含压缩、取消）的计量事件；验证 Trace 保存在 `artifacts/pi-step0/live-trace.sqlite3`。原始推理不导出 OTLP；流式探针只记录推理长度，完整消息经私有 SQLite 恢复实验验证。

`replay` 显式读取实验历史作为离线数据；生产工具不依赖实验目录。需要本机 `experiments/experiment_2_hil_tools/results/sanya_live_20260910T094400/`，该目录缺失时失败，不能通过在线查询或新造数据补齐。测试把旧 summary 投影为紧凑工具结果，并给旧地点补充可引用的 `place:<fixture>` ID；不代表当前主工具已迁移。参数不匹配返回 `NoHistoricalData`。网络重试和 SDK 自动压缩均关闭，压缩恢复场景显式调用压缩。保留全部运行，不筛掉低覆盖率或失败样本。

所有运行结果写入忽略的 `artifacts/pi-step0/`；可公开的计量与判定摘要由报告引用。Pi faux 的 token 是其模拟统计，不能用于 DeepSeek 预算。Pi usage.input 不含 cacheRead，实际输入需将两者相加；取消请求的本地零用量不代表供应商没有收费。
