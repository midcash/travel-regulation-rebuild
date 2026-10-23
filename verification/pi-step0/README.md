# Pi 接入探针

这是隔离验证代码，不是产品运行时。Pi SDK 精确锁定 1.3.0；当前产品入口和支持范围见 [根目录 README](../../README.md)，协议约束见 [离线测试](test_portable.py)。默认检查不需要凭证或本机历史数据。

先在仓库根目录安装并运行：

```powershell
npm.cmd ci --prefix verification/pi-step0 --ignore-scripts --no-audit --no-fund
python scripts/check.py
```

只运行协议测试：`python -B -X utf8 -m unittest discover -s verification/pi-step0 -p test_portable.py -v`。另可运行 `python -X utf8 verification/pi-step0/run.py offline`，不落盘结果。默认测试走真实 Node/SDK、合成模型/工具及临时 SQLite；旧 HIL 兼容和私有历史回放分支已删除。

仅在显式核对真实模型接入时执行 `python -X utf8 verification/pi-step0/run.py probe` 或 `live`。两者读取现有 `.env`、调用 DeepSeek 并收费；`live` 需先安装 `python -m pip install -e ".[otel]"`，验证本地 OTLP。它们只使用合成工具，绝不证明真实差旅供应商/页面验收。付费探针的 [初始化代码](run.py) 仍固定历史验证型号 `deepseek-flash`，不等于正式产品选择的型号；执行前须核对该型号当前可用性及供应商价格，并说明预估成本和调用范围。正式产品真实链路另见 [航班基线验收](../baseline/README.md)，不要把历史型号或 SDK 零价元数据当作现行计价依据。当前产品有每轮调用次数、输出 token 和执行时间上限，不提供金额或日累计预算的硬限额。

付费输出写 `artifacts/pi-step0/`；默认检查只使用系统临时目录。Node 配置/工作目录均重定向，Pi 会话仅在内存；文件钩子是回归探针，不能等同正式系统沙箱。`pi0_` 表只属于临时验证库，不是生产会话 schema。
