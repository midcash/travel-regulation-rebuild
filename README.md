# 差旅助手

当前仓库提供本地浏览器航班查询基线：FastAPI 托管页面，Pi SDK 1.3.0 运行在 Node Worker 中，调用 Python `RealTools` 查询航班。支持同一内存会话追问、结果卡片、刷新续聊与取消。**这是航班纵向切片，不代表 B01–B12 或完整 MVP 已验收**。不会创建订单或付款。

## 业务依据

- [CONTEXT.md](CONTEXT.md)：领域术语与语义。
- [已确认的业务行为与验收场景](docs/product-behavior.md)：B01–B12。它们是目标行为，不代表当前已全部实现。

## 环境与默认检查

需要 Python 3.11+、Node 22.19+ 和 npm。在 Windows PowerShell 的仓库根目录执行：

**本版仅支持从完整仓库源码运行，使用 editable 安装 Python 包，并分别通过根目录和 Pi 探针目录的 npm 锁文件安装依赖。** 请保留源码目录与各自的 `node_modules`；启动时以仓库根目录为工作目录。Python wheel 不包含 Pi 的 npm 运行依赖，独立 wheel 安装不属于本版支持的交付方式。仅安装 Python 包不能替代下列 npm 安装步骤。

```powershell
python -m pip install -e .
npm.cmd ci --ignore-scripts --no-audit --no-fund
npm.cmd ci --prefix verification/pi-step0 --ignore-scripts --no-audit --no-fund
python scripts/check.py
```

默认检查运行工具测试与 Pi 离线进程测试，不需要真实密钥，不调用在线供应商，也不产生真实模型费用。测试通过不等于正式产品或真实端到端验收通过。

## 运行浏览器基线

在现有 `.env` 或环境变量中配置模型和航班供应商（字段见 `.env.example`），然后运行：

```powershell
python -B -X utf8 -m travel_agent
```

打开 [行旅](http://127.0.0.1:8765)，先说“我要去三亚”，再补充出发城市与日期。也可以一次提供完整查询条件。查询使用真实接口，可能产生模型及供应商费用；未知价格不会补零。

仅支持单用户、单服务进程（不要使用多个 Uvicorn workers）。每个会话串行运行，90 秒总执行预算，单次供应商查询不超过 45 秒；取消先中止 Pi，已发出的供应商请求在超时内收尾。SSE 重连和页面刷新不会发起新模型调用。正常完成后复用 Node Worker，服务退出时回收；Worker 异常时明确提示上下文丢失。页面只转发筛选过的状态、答复和航班事实，不转发内部思考与原始供应商响应。

会话只保存在内存，服务重启后不恢复，**B08 尚未完整满足**。历史卡片不是当前报价。本版不接入酒店、火车、路线、订单付款或多用户；现有工具与 Trace 功能保留。新增验收工具及运行方式见 [verification/baseline](verification/baseline/README.md)。真实航班证据保存在 `artifacts/baseline/<session-id>/`，不作为会话数据库。

## 真实模型探针

探针说明与命令见 [verification/pi-step0/README.md](verification/pi-step0/README.md)。真实模型调用可能产生费用，必须显式执行并先估算成本；该探针使用合成工具，不调用真实差旅供应商，也不代表产品验收通过。

## 密钥与运行数据

真实调用配置示例见 `.env.example`。不要提交密钥；默认检查不加载 `.env`。本机历史运行记录和旧数据库不属于代码清理范围，不要因文档或源码调整而迁移、覆盖或删除。
