# 四个工具实验

本目录从 `F:/Commercial project/skill` 迁移政策读取、城际交通、市内驾车路线、酒店检索。城际交通分别运行航班和高铁，因此一次完整实验会输出五项结果。实现只依赖本目录和 Python 第三方包，不导入原仓库源码，不接入主链路，不使用 LLM。

## 直接运行与调试

当前项目的 `.venv` 已补齐依赖，来自原 `skill/venv` 中已安装的包，`pip check` 已通过。密钥只复制了 `AMAP_API_KEY`、`TUNIU_API_KEY` 到本目录 `.env`；进程环境变量优先于该文件。`.env`、虚拟环境和运行结果均被 Git 忽略。

PyCharm 选择解释器 `F:/Commercial project/travel-regulation-rebuild/.venv/Scripts/python.exe`，直接运行本目录 `run.py`。默认查询中国时间今天往后第 3 天，北京到上海的航班/高铁、上海一晚酒店，以及固定的上海市内坐标之间的驾车路线。日期是实验样例，不是用户行程。

在项目根目录的 PowerShell 中运行：

```powershell
.\.venv\Scripts\python.exe -m experiments.external_tools.run
.\.venv\Scripts\python.exe -m experiments.external_tools.run --tool train --date 2026-09-12
.\.venv\Scripts\python.exe -m experiments.external_tools.run --tool hotel --destination 上海
.\.venv\Scripts\python.exe -m pytest experiments/external_tools/test_tools.py -q
```

`--tool` 可选 `all / policy / flight / train / route / hotel`。`--origin`、`--destination` 修改城际交通城市；`--destination` 同时修改酒店城市。路线坐标独立，在 `run.py` 的 `RouteQuery` 中修改，不随城市参数变化。`--timeout` 默认 30 秒。只需要改查询参数、打断点即可观察请求和结构化结果。

每次运行在 `results/<中国时间时间戳>/` 保存各工具的输入、成功结果或错误、耗时以及 `summary.json`。网络失败不会切换模拟数据；任一项失败，总体退出码为 1。结果仅是当次观测，不是长期有效的票价或库存。

重新创建环境时，用 Python 3.11 及以上创建 `.venv`，然后执行 `python -m pip install -r experiments/external_tools/requirements.txt`，并按 `.env.example` 填入密钥。

## 建议阅读顺序

- `run.py`：五个具体查询和最小调用流程，适合作为 PyCharm 调试入口。
- `models.py`、`values.py`：输入与标准返回值，使用 Pydantic 校验，金额使用 Decimal。
- `policy.py`、`policy_models.py`、`policy.md`：本地政策解析、结构和示例。
- `tuniu.py`：航班、火车、酒店请求和返回解析，共用一个客户端实现。
- `amap.py`：高德驾车路线。
- `config.py`、`errors.py`、`clock.py`：必要配置、明确错误类型和观测时间。
- `test_tools.py`：固定供应商样例的离线合同验证，不需要 API Key。

保留原实现中有关 JSON-RPC/SSE、响应大小、字段校验和异常分类的逻辑，删除了装配工厂、遥测、地理编码、门票及其他未使用能力。后续集成直接调用 `PolicySnapshotLoader.load(path)` 或 `provider.search(query, timeout_seconds=Decimal("30"))`，由调用方管理 `httpx.AsyncClient` 的生命周期即可。

## 政策读取的边界

仅从本地 UTF-8 Markdown 读取受控格式，不做向量检索、自然语言理解或政策冲突判断。文件必须有六项元数据、`## 规则` 标题及固定的五列表头，参考 `policy.md`。返回政策元数据、规则、来源路径以及原文件字节的 SHA-256。

规则值只自动解析布尔、整数和字符串，小数及条件表达式仍是字符串。读取成功不代表政策在某个行程日期有效：有效期和适用对象由下游判断。规则 ID 唯一；文件缺失、非法 UTF-8、格式错误均为 `PolicyDocumentError`，缺失规则查找为 `KeyError`。

## 城际交通的边界

途牛 `searchLowestPriceFlight` / `searchLowestPriceTrain` 只收到出发城市、到达城市和中国时区的出发日期。`departure_after` 的小时分钟不会形成供应商过滤条件，`arrival_before` 也不会传入或在适配器中筛选；`travelers` 不会传给交通接口，报价不会乘人数。跨日到达是合法返回，下游必须再次检查出发/到达时间窗。

航班报价是 `basePrice + totalTax`；火车报价是支持座席字段中的最低正数，可能来自站票等席别，不保证二等座、可售余票或满足座席偏好。`high_speed_rail` 仅以 G/D/C 车次前缀筛选，包含动车、城际及可能的夜间动车，不能等同于“最快高铁”。航班没有机场字段，火车保留出发和到达站名；仅有城市不足以直接连接市内路线。

`max_results` 截取供应商顺序，不进行全量比价、分页或排序。单个已解析记录非法会使该次查询失败；高铁无匹配为 `ToolConstraintNoMatchError`，空列表为 `ToolEmptyResultError`。不支持指定航空公司、机场、席别、换乘组合、返程或预订。航班模式下 `service_class` 不起筛选作用，调用方应使用 `any`。

## 市内交通的边界

只有高德驾车路线，输入为两个不同的 **GCJ-02** 坐标，按“经度,纬度”发送。原文档和原 `GeoPoint` 注释写成 WGS84，但原代码没有执行转换；本实验纠正为 GCJ-02 并拒绝显式传入其他坐标系。默认值不能识别一组数值的真实坐标系，调用方必须保证来源正确。依据：[高德官方坐标系说明](https://developer.amap.com/faq/advisory/others/39838)。

返回距离（米）、耗时（秒）、固定为 0 的换乘次数及来源，不含出租车费用、分段道路、地铁、公交、步行或骑行。没有出发日期参数，不能把当次耗时当作未来某天的实时路况预测。预计到达时刻和安全缓冲由下游计算。实体 ID 按本次路径顺序编号，不是跨查询唯一的道路标识。

实验使用的上海坐标只是路线连通性样例，未证明它们是本次航班机场、火车站或酒店的坐标。当前四工具尚不能直接闭合“交通站点 → 酒店 → 会议地点”的路线链路，需要下游另行提供可信坐标。

## 酒店的边界

途牛 `tuniuHotelSearch` 支持城市、入住/退房日期、成人数、关键词和供应商价格区间；退房须晚于入住，仅支持 CNY 价格条件。`area` 作为搜索关键词发送，但结果的 `area` 是供应商返回的城市名，不表示已验证的商圈或行政区。

保留兼容字段 `total_price`，其值实际直接来自 **`lowestPrice`**，没有乘晚数、人数或房间数。当前证据不能确认它是整段住宿总价；`max_total_price` 也只是转发给供应商的价格筛选，不能作为整段行程预算已满足的证据。不要直接据此得出住宿合规结论。

坐标和可退状态固定为 `None`，没有房型、早餐、床型、星级、房间数和库存保证。入住退房时刻由查询日期投影为中国时区零点，不是酒店实际办理入住退房的时间。不支持预订、付款或取消。

## 错误与集成约定

网络返回包含 `query_id`、`provider`、`operation`、`observed_at`、`source_ref`、`items`；失败抛出明确异常而不是伪造空成功。鉴权、限流、超时、连接失败、业务失败、格式异常及空结果在 `errors.py` 中区分。HTTP 重定向不跟随，每次调用不重试、不缓存、不回退。

响应上限 1 MiB；途牛支持 JSON 或一个 SSE 事件，不是通用 MCP 会话客户端，也不支持任意流式事件序列。适配器参数 `timeout_seconds` 是 httpx 各网络阶段超时；`run.py` 额外用 `asyncio.timeout` 限制整个调用时间。主链路如有总预算，也需在调用外设置墙钟超时。

外部文本经过 HTML/控制字符清洗并标记 `untrusted`，仍不可作为指令执行。运行入口只保存安全错误摘要，不保存原始供应商响应、请求头或密钥；异常链可能含 HTTP 库的底层请求信息，集成时不要将完整 traceback 直接输出到公开日志。

相较原实现，本实验还修正了：自定义端点下单项结果的来源引用、UTC 跨日出发日期、路线字段异常的错误分类与 operation、非法 UTF-8 政策错误封装、Windows 换行导致的原文哈希差异。主项目代码和原 `skill` 源码均未修改。

## 验证记录

最终真实调用使用当前项目 `.venv`，记录见 `results/20260909T112433286217/summary.json`：政策成功读出 6 条规则，航班 3 条、高铁 3 条、驾车路线 1 条、酒店 3 条，整体退出码 0。查询日期为 2026-09-12，酒店住一晚；这些是合成实验参数。

26 个离线测试全部通过，验证价格数值、实际请求参数、时间窗不筛选的行为、高铁前缀筛选、酒店缺失字段、路线单位、HTTP 错误、业务错误、空结果、非法金额、政策哈希及输入拒绝。额外在独立进程故意漏算航班税费，JSON/SSE 两个金额断言均如预期失败；正常实现未修改，随后复跑仍是 26 项通过。测试使用固定样例，不能替代在线服务可用性证据。独立审查子任务因额度限制未完成，不将其算作验证通过。

实验源码与说明纳入版本管理；`.env`、`.venv` 和 `results/` 保留在本地，不进入提交。可直接在 PyCharm 查看和修改源码。
