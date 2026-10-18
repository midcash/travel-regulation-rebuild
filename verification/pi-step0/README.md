# Pi 接入探针

这是隔离验证代码，不是产品运行时。Pi SDK 精确锁定 1.3.0；保留结论见 [第 0 步记录](../../docs/pi-step0-report.md)。默认检查不需要凭证或本机历史数据。

先在仓库根目录安装并运行：

```powershell
npm.cmd ci --prefix verification/pi-step0 --ignore-scripts --no-audit --no-fund
python scripts/check.py
```

只运行协议测试：`python -B -X utf8 -m unittest discover -s verification/pi-step0 -p test_portable.py -v`。另可运行 `python -X utf8 verification/pi-step0/run.py offline`，不落盘结果。默认测试走真实 Node/SDK、合成模型/工具及临时 SQLite；旧 HIL 兼容和私有历史回放分支已删除。

仅在显式核对真实模型接入时执行 `python -X utf8 verification/pi-step0/run.py probe` 或 `live`。两者读取现有 `.env`、调用 DeepSeek 并收费；`live` 需先安装 `python -m pip install -e ".[otel]"`，验证本地 OTLP。它们只使用合成工具，绝不证明真实差旅供应商/页面验收。付费探针仍固定历史验证型号 `deepseek-flash`；正式型号、价格及日累计控制见 [Pi 方案](../../docs/pi-migration-plan.md)，不要把历史型号或零价元数据当作现行计价依据。

付费输出写 `artifacts/pi-step0/`；默认检查只使用系统临时目录。Node 配置/工作目录均重定向，Pi 会话仅在内存；文件钩子是回归探针，不能等同正式系统沙箱。`pi0_` 表只属于临时验证库，不是生产会话 schema。
