# Pi Durable 能力实验室

本轮核心是独立的 Pi Durable MVP：在本机浏览器验证对话、工具调用、持久化笔记、取消、后台计时与服务重启后的继续执行。默认使用明确标注的离线合成模型，不查询差旅供应商，不连接 MCP。真实 DeepSeek 由 **pi-ai provider** 接入，再交给 Pi Durable，Pi Durable 是唯一维护的 Agent 运行路径。

产品方向为个人日程 Agent 助手，差旅作为保留的子能力。支持会话读取、新增、修改、删除和改期日程及 FullCalendar 视图；确认目标见 `docs/product-behavior.md` 的 S01–S05。

这是能力 MVP，不代表完整产品或真实模型端到端验收通过。[CONTEXT.md](CONTEXT.md) 定义业务术语，[docs/product-behavior.md](docs/product-behavior.md) 定义日程与差旅子能力的已确认目标。

## 安装与启动

需要 Python 3.11+、Node 22.19+ 和 npm。仅支持从完整仓库源码运行；Python wheel 不包含 Node 应用和依赖。

Python 命令统一在仓库自带的 `.venv` 中执行，不要使用系统 Python 或其他解释器。Windows PowerShell 可先运行 `.venv\Scripts\Activate.ps1`，再用 `python`；未激活时用 `.\.venv\Scripts\python.exe` 代替。CI 使用自己创建的环境，不依赖本机 `.venv`。

```powershell
python -m pip install -e .
npm.cmd ci --prefix durable_mvp --ignore-scripts --no-audit --no-fund
python -B -X utf8 -m travel_agent
```

打开 [能力实验室](http://127.0.0.1:8765)。输入普通消息观察离线回显；`/note 一条笔记` 保存笔记，`/slow 一段内容` 执行十秒工具，`/fail` 演示工具失败。页面可创建十秒后台计时。刷新不会提交新消息；关闭服务后用相同命令重启，会话、笔记和后台任务继续保留。停止服务请在运行命令的终端按 Ctrl+C，启动器和 Node 宿主会一起退出；强制结束 Python 启动器可能留下仍在运行的 Node 宿主，此时再次启动会报 `Store is already in use`，结束占用该端口的 `node` 进程后即可重启。

默认数据位于新的 `.durable-mvp/offline/`，不读取或迁移旧数据库。可指定独立的新目录和端口：

```powershell
python -B -X utf8 -m travel_agent --data C:/path/to/new-mvp-store --port 8765
```

### 日历视图与工具

项目统一采用固定 UTC+8（IANA `Etc/GMT-8`，+08:00，不随夏令时变化）：页面显示时区、日历工具默认时区、模型提示与离线示例一致，差旅侧的时间解析同样按 +08:00。页面使用本地安装的 FullCalendar 6.1.20（MIT），支持月、周、日视图；显式加载中文语言包。点击事件查看完整起止时间。四个原生工具为 `list_calendar_events`、`create_calendar_event`、`update_calendar_event` 和 `delete_calendar_event`，与页面 HTTP 查询共用 Durable 文档及 `session.sqlite`，不另建数据库、不迁移旧数据。

离线模式示例（仅合成模型指令，不代表自然语言理解）：

```text
/calendar {"title":"项目评审","start":"2026-11-06T15:00:00+08:00","end":"2026-11-06T16:00:00+08:00","timeZone":"Etc/GMT-8"}
/events {"start":"2026-11-06T00:00:00+08:00","end":"2026-11-07T00:00:00+08:00"}
```

真实模式使用自然语言。缺少标题、日期或开始/结束时间时先追问；未另行指定时区时采用已确认的 UTC+8，不默认补时长。工具校验时间与 IANA 时区一致，按半开区间判断重叠；重叠时保存候选并展示确认按钮并列出冲突，用户点击后通过会话提交 `确认重叠 <标识>`，服务端记录授权，工具重新核对当前冲突；出现未展示的新冲突时更新清单并要求再次确认，否则才创建。支持通过会话按查询所得 ID 修改和删除日程；修改保留未指定字段，改期保留原 ID，重叠时确认后才替换原安排。匹配不唯一时先追问。暂不支持全天、重复日程或提醒。

离线可用 `/update {"eventId":"查询返回的 ID","title":"航班"}` 和 `/delete {"eventId":"查询返回的 ID"}` 验证工具；自然语言及多工具自主组合需使用真实模型，离线命令不代表语言理解验收。

相同 HTTP 请求 ID 沿用现有提交去重；日程、创建工具 call ID 对应的结果和变更记录在同一 Durable 事务提交，恢复重放返回原结果。确认候选保存原日程 ID 和最终结果，重复确认也不会新增事件。新请求表达相同内容不会自动视为重复，而会走重叠确认。上限为 200 条已创建日程。

### 真实模型（主动选择，可能产生费用）

在本地 `.env` 或环境变量配置 `DEEPSEEK_API_KEY`、`DEEPSEEK_MODEL` 和可选的 `DEEPSEEK_BASE_URL`：

```powershell
python -B -X utf8 -m travel_agent --mode deepseek
```

仅真实模式由 Python 启动器加载 `.env`；离线模式不加载。真实模式用自然语言要求保存笔记等。锁定版 pi-ai 默认模型为 `deepseek-flash`，还支持其目录中的 `deepseek-v4-pro`。旧配置若为 `deepseek-v4-flash`，需由用户更新；启动错误会列出支持的模型，不自动猜别名或价格。自定义地址仍由 pi-ai 传输和认证。真实模式使用单独的新目录 `.durable-mvp/deepseek/`。

重启会自动恢复未完成任务，模型请求可能重新发出并产生费用。页面调用数只统计本进程，不是跨重启账单。本轮未执行真实模型调用。

## 源码与能力边界

- [durable_mvp/server.mjs](durable_mvp/server.mjs)：HTTP、pi-ai、Pi Durable Harness、SQLite、原生工具和任务。
- [durable_mvp/calendar.mjs](durable_mvp/calendar.mjs)：日历文档、查询、新增、修改与删除工具、重叠候选和事务去重。
- [页面](durable_mvp/index.html)、[交互](durable_mvp/app.js)、[样式](durable_mvp/style.css)：左中右三栏；输入框固定在对话区底部，日程与重叠确认在右侧可伸缩侧边栏，笔记、计时、命令说明和运行信息在 `#/notes` 视图。交互仍轮询保存的结果，并提交消息、取消和后台计时操作。
- [启动入口](travel_agent/__main__.py)。
- [本轮验收](verification/durable-mvp/README.md)：运行方式、断言和浏览器观察。
- [底层 Durable 探针](verification/durable-step0/README.md)：原子性、存储失败和已提交状态等已有验证。

Pi Durable、pi-ai 和 chord 精确锁定 1.4.0；Durable 是实验性 API。只支持单机、单用户、单会话、单进程，同一数据目录拒绝第二个宿主：宿主用独立 SQLite 锁库的独占事务声明占用，进程退出或被杀后由系统释放，不依赖 pid 文件，也不需要手工清理。MVP 上限为 200 个请求 ID、20 个后台计时。框架负责请求去重，应用持久化 ID 与正文绑定，拒绝同 ID 不同正文。浏览器对未确认提交保留原 ID，可以显式重试。

`save_note` 通过框架事务同时写笔记和变更记录，用工具 call ID 防止恢复时重复写入。`slow_echo` 声明可安全重放，强杀后可能重新执行。取消本轮中止普通工具，保留后台计时。停机期间计时任务不会执行，重启后补做已到期任务。安全重放不等于任意外部副作用恰好一次；未来 MCP 工具需要逐个决定重放策略。

不包含多用户、生产级运行预算、备份、数据库迁移、MCP、真实差旅查询和完整 B01–B12 验收。恢复能力只为 B08 提供底层依据，不表示差旅选定项及方案已验收。

## 默认离线检查

安装 MVP 依赖后，完整检查还需安装 Durable 探针依赖：

```powershell
npm.cmd ci --prefix verification/durable-step0 --ignore-scripts --no-audit --no-fund
.\.venv\Scripts\python.exe scripts/check.py
```

激活虚拟环境后等价于 `python scripts/check.py`。Windows 需允许探针强杀自己启动的 Node 子进程。检查不加载 `.env`，不产生模型或供应商费用。CI 在 PR 和 main 推送时运行，已增加 MVP 依赖安装；远端结果另行核实。

## 保留的业务资产与数据

差旅子能力的 [RealTools](travel_agent/real_tools.py)、交通/住宿/地点/路线供应商适配、[事实结构](travel_agent/facts.py)、[传输边界](travel_agent/supplier_wire.py) 和 [Trace](travel_agent/telemetry.py) 保留，有独立工具测试，尚未接入 Durable。Trace 不等于业务会话恢复。

[差旅子能力待接入验收索引](verification/baseline/README.md) 区分底层工具与未验证业务行为。决策记录：[退役 ADR](docs/adr/001-retire-coding-agent.md) 记录历史位置，[ADR 002](docs/adr/002-pivot-to-schedule-assistant.md) 记录转向个人日程助手的决定。

不提交密钥、.env 或本机记录；历史数据库、Trace、experiments 和个人数据不迁移、覆盖或删除。
