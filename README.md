# 差旅规划 Agent · 第一阶段

以 `F:\Commercial project\observation` 为基底：保留 agent loop、SQLite / 可选 OTLP、评测执行与质量门禁、运行观测 / 回归评测 / 全链路三个页面，只替换差旅业务。

LLM 默认使用原项目相同的真实 DeepSeek 配置；主链路中的公司政策、城际交通、酒店、市内交通仍由四个本地工具返回固定演示数据。独立实验目录提供途牛交通与酒店、高德地点与驾车路线的全字段真实查询，尚未接入主链路；政策工具本轮不处理。不支持预订或付款。规则基线只是显式选择的离线对照，不会在模型失败时自动接管。

## 本地启动与 PyCharm

主链路已增加可选人工交互：网页勾选“启用人工交互”后，可确认需求、选择已查询候选并补充偏好；等待状态可跨服务重启恢复。默认非交互入口保持原行为。见 [HIL 使用说明](docs/hil-usage.md)与[阶段验收记录](docs/hil-implementation-progress.md)。

当前工作区已建立 `.venv`，已安装项目与可选 OTLP 依赖。直接运行：

```powershell
.\.venv\Scripts\python.exe main.py
```

打开 <http://127.0.0.1:8766>。使用 8766，避免占用 observation 常用的 8765。

PyCharm 解释器选择当前项目的 `.venv\Scripts\python.exe`，脚本选择 `main.py`，不填参数即可启动真实 DeepSeek 工作台。工作目录由入口定位到项目根目录；可在 `travel_agent/agent.py` 的 `provider.chat`、`toolbox.execute`、`validate_verdict` 附近打断点。调试模型决策时暂停太久可能触发原有 180 秒运行预算。

本地 `.env` 已沿用 observation 配置，密钥不会被 Git 跟踪或写入 Trace。新建环境时：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
# 可选：需要 OTLP 时安装；SQLite 和三个页面不依赖此扩展
.\.venv\Scripts\python.exe -m pip install -e ".[otel]"
Copy-Item .env.example .env
# 在编辑器中填写 .env 的 DEEPSEEK_API_KEY，再启动
```

`start-local.ps1` 也可启动；离线查看使用 `./start-local.ps1 -Provider baseline`。

## 演示业务

默认案例：2026-09-12 08:00 在上海东方明珠塔开会，杭州出发。

- 前一天 19:00–20:30：杭州东站 → 上海虹桥站，模拟动车二等座，73 元。
- 20:50–21:20：上海虹桥站 → 桔子水晶酒店，模拟出租车，120 元。
- 21:20 至次日 07:20：模拟住宿一晚，400 元。
- 次日 07:20–07:50：酒店 → 东方明珠塔，模拟出租车，35 元。

模拟总价 628 元。演示政策为前一天 18:00 起出发、酒店每晚 ≤500 元、总价 ≤1000 元、至少提前 10 分钟到达。不计算出发地到出发车站及站内时间。酒店和交通时间均是测试数据，不代表真实报价或路线。

页面保留“结构化自定义会议”表单，并新增自然语言请求入口。自然语言仅解析当前业务需要的会议日期/时间、出发地、会场、飞机或火车偏好和前一晚住宿；相对日期按提交时捕获的亚洲/上海时间解析并显示。解析出的会议或前一日到达/入住日期已过时会要求更正，不会调用供应商。真实结果会注明默认 1 人 1 间房。演示数据仍只覆盖“杭州 / 上海 / 东方明珠塔”；不建设通用对话、多方案搜索或路线优化。

## 四个工具的独立实验

入口位于 [experiments/external_tools](experiments/external_tools/README.md)，运行时不依赖原 `skill` 源码。PyCharm 使用当前项目 `.venv\Scripts\python.exe`，直接运行 `experiments/external_tools/full_run.py`。完整保留供应商字段与嵌套结构，后续集成再选择业务所需数据。

```powershell
# 新环境安装实验依赖，并在实验目录 .env 中填写两个 API Key
.\.venv\Scripts\python.exe -m pip install -r experiments/external_tools/requirements.txt
# 配置格式参考 experiments/external_tools/.env.example
# 全部真实只读查询，默认日期为中国时间今天往后第 3 天
.\.venv\Scripts\python.exe -m experiments.external_tools.full_run
# 单独测试一个服务；可选 flight / train / hotel / amap，政策不运行
.\.venv\Scripts\python.exe -m experiments.external_tools.full_run --only train
# 离线合同测试，不调用供应商
.\.venv\Scripts\python.exe -m pytest experiments/external_tools/test_capture.py -q -o addopts= -p no:cacheprovider --basetemp=experiments/external_tools/results/pytest-local
```

2026-09-09 完整全字段样本执行47次请求，45次成功；航班周边机场两种模式返回供应商业务错误并保留记录。已查询搜索、分页、航班舱位/火车席别/酒店房型详情、地点与扩展驾车数据。每次原始响应、解析结果、字段覆盖率和错误保存在 `results/full_<时间戳>/`，该目录及密钥文件不进入 Git。字段未返回、字段为空和请求失败分别记录，不宣称所有可选字段都必然有值。

原 `run.py` 和业务 DTO 保留作对照，不代表全字段输出；其中政策路径仍指向已删除的旧文件，不能继续把旧政策成功记录当成当前状态。全字段入口不运行政策。完整运行方法、数据口径和已知限制见 [实验说明与能力边界](experiments/external_tools/README.md)。

## 开发与验证

```powershell
# 无网络测试，包括本地 HTTP 与已安装时的 OTLP 导出测试
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
# 六个固定场景的离线回归
.\.venv\Scripts\python.exe -m travel_agent eval --provider baseline
# 真实模型回归（调用 API）
.\.venv\Scripts\python.exe -m travel_agent eval --provider deepseek --repeats 2
# 单次运行 / 查看 Trace
.\.venv\Scripts\python.exe -m travel_agent run --provider deepseek --scenario hangzhou-shanghai
.\.venv\Scripts\python.exe -m travel_agent trace TRACE_ID
```

六个案例覆盖成功、重试恢复、酒店超预算、到达过晚、无酒店、连续超时。`evals/cases.json` 中的 oracle 不进入模型或工具上下文，程序分别检查结果、证据、行程、时间、费用与重试。门禁沿用原项目：任务通过率和安全断言通过率均为 100%、P95 ≤300 秒、最多 12 次工具调用。agent loop 仍保留 10 轮、12 次工具调用、180 秒总时限、45 秒单次模型超时、24000 Token 的默认限制。

观测数据位于 `artifacts/observability.sqlite3`，CLI 评测报告默认为 `artifacts/latest-evaluation.json`；页面评测保存到数据库。`artifacts`、`.env`、`.venv` 和 `.idea` 均不提交。失败历史保留用于比较，不因修正而清空。费用只有在 `.env` 配置了三个计价字段时才显示，未配置时显示“未计价”。

阅读顺序建议：`agent.py` → `tools.py` / `domain.py` / `prompts/planner.txt` → `telemetry.py` → `evaluation.py` → `server.py` / `web`。详见 [验收约定](docs/acceptance.md) 和 [验证记录](docs/verification.md)。

## Git 回退

开发分支：`codex/travel-phase1`。`55b8347` 是 observation 源码基底快照，用来比较保留与替换了哪些内容；该快照尚未完成差旅包配置，不作为可运行差旅版本。第一阶段完成提交是后续业务优化的可运行回退点。使用 `git log --oneline` 查看提交；回退业务修改优先通过 `git revert` 生成反向提交，保留历史。
