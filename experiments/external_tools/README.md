# 四个工具实验

本目录独立验证供应商能力，不接入主链路、不使用 LLM。当前入口是 **`full_run.py`**：完整解析国内航班、火车、酒店以及高德地点/驾车接口返回的所有字段，不按差旅业务裁剪。政策工具保持原状，本轮不读取或修改政策。

## 当前全字段实验：运行与阅读

PyCharm 选择当前项目 `.venv/Scripts/python.exe`，直接运行 `full_run.py`。沿用本目录 `.env` 的 `TUNIU_API_KEY`、`AMAP_API_KEY`，无需新依赖。也可在项目根目录运行：

```powershell
.\.venv\Scripts\python.exe -m experiments.external_tools.full_run
# 仅验证某个供应商服务：flight / train / hotel / amap
.\.venv\Scripts\python.exe -m experiments.external_tools.full_run --only hotel --nights 2 --details 3
# 每次响应中的记录全部保留；增加采样页数与详情样本数
.\.venv\Scripts\python.exe -m experiments.external_tools.full_run --max-pages 5 --details 5
# 离线验证新解析器及旧网络适配器，不运行政策测试
.\.venv\Scripts\python.exe -m pytest experiments/external_tools/test_capture.py experiments/external_tools/test_tools.py -k "not policy" -q -o addopts= -p no:cacheprovider --basetemp=experiments/external_tools/results/pytest-local
```

默认北京到上海、中国时间今天往后第三天、住宿两晚、上海国际会议中心周边酒店。`--date`、`--origin`、`--destination`、`--meeting`、`--nights` 可修改样例条件；单独运行高德时，`--hotel` 是酒店地点样例。默认每项搜索最多两页、前三类服务各两条详情，`--details 3` 可覆盖三条。限制的是采样次数，不是响应字段或每次响应的记录数。`--timeout` 是单次请求总超时，默认45秒。

本轮的“全部”指以下只读接口的完整响应，包含图片链接、退款规则、办理时间、库存、预订参数等当前业务可能不用的内容；不等同于遍历供应商整个平台。订单创建、付款、取消和已有订单详情不属于本轮查询实验；不会为了取得订单字段创建真实订单。

- 航班：`tools/list`、`searchLowestPriceFlight` 六种查询模式、默认搜索分页、`multiCabinDetails`、`getBookingRequiredInfo`。
- 火车：`tools/list`、`searchLowestPriceTrain` 及分页、`queryTrainDetail`。
- 酒店：`tools/list`、`tuniuHotelSearch` 及分页、`tuniuHotelDetail`。详情 ID 来自实际搜索；完整保留全部房型和全部报价。
- 高德：POI 2.0 关键字检索、ID详情、周边检索、地理编码、逆地理编码、v3/v5驾车。POI 请求全部 `children,business,indoor,navi,photos`，v3 请求 `extensions=all`，v5 请求全部 `cost,tmcs,navi,cities,polyline`。

流程为“搜索 → 从结果取 ID 查详情 → 取酒店/站点/机场名称查地点 → 用实际 POI 坐标查路线”。地点候选全部保存；排序第一项仅用于接口连通性实验，不表示已确认跨供应商是同一实体，也不生成会前可达的业务结论。`place_candidates.json` 和 `route_chain.json` 记录选择依据。机场、车站到酒店及酒店到会议地点的路线样本分别记录。

建议依次阅读：`full_run.py`（调用过程）→ `capture.py`（HTTP、JSON/SSE、全字段保留）→ `field_catalog.py`（文档字段清单）→ `results/full_*/`（真实数据）。没有增加业务 DTO 或复杂适配层：Python 中的完整结果是字典/列表，后续集成再按实际需求映射。

## JSON 输出内容

每次运行独立保存在 `results/full_<时间戳>/`：

- `summary.json`：调用清单、状态、参数、父查询关联和每次结果文件。
- `001_*.json` 等：单次完整结果，`wire_text` 保留接收原文，`envelopes` 保留 JSON-RPC/HTTP 结构，`payloads` 展开所有内容块中的 JSON，同时保留普通文本及其他内容块。
- `field_catalog.json`：本轮采用的官方文档字段与来源。
- `coverage.json`：文档字段的实测状态，以及不在清单中的额外字段路径。
- `*_pagination.json`：供应商报告页数、已取得页数、是否完成报告的全部分页。

`capture.py` 不使用字段白名单裁剪数据。`field_catalog.py` 只用于比较，未知字段、额外嵌套结构、空值、原始供应商 ID 都保留。`tools/list` 的完整方法描述、输入/输出 Schema（若提供）也会落盘。公开网页和在线 Schema 可能不同，不能把清单当成供应商永远不变的严格结构。

覆盖状态含义：`observed` 表示至少一个样本非空；`empty_or_null_only` 表示出现但全部为空；`not_returned_in_samples` 表示成功样本未带此字段；`not_verified` 表示没有成功样本。**未返回不等于供应商不支持，出现过也不等于每个候选都有。** 数组字段统计包含各内容通道的观测次数，不能拿它当酒店或航班数量；同一载荷在 `content` 和 `structuredContent` 中重复时均保留。

JSON 数值小数按 `Decimal` 解码，落盘为精确十进制字符串；`field_inventory.types` 区分供应商原本的 decimal/string，整数保持整数，原始数值写法仍在 `wire_text`。不把坐标字符串、金额字符串或供应商 ID 擅自转换为业务值，不改名、不推断单位、不合并价格。

每个调用有独立 UUID 和父调用 ID。错误响应也保存，任何请求失败时整体退出码为1；这不删除其他成功结果。空列表可以是一次成功查询，但不会被描述为取得了候选。单次响应上限16 MiB；超限、超时或格式异常明确记录失败，不当作完整结果。

JSON/SSE 支持心跳、通知、多行事件、多个内容块，以及途牛 `structuredContent.result` 内的 JSON 字符串；读取到匹配本次请求 ID 的完整结果即结束，不宣称实现所有 MCP 会话协议。失败时也保留已收到的响应文本。所有供应商文本（包括 `displayHint`）都是待分析数据，不执行其中指令。

`.env` 和 `results/` 不进 Git；不写请求头。本地响应可能含短期有效的报价/预订参数，按用户本次要求完整保留。供应商若回显 API Key，普通或 Unicode 转义形式会被遮盖；此时对应文本不再是原始响应的完整副本。

## 全字段实测记录（2026-09-09）

完整流程样本：`results/full_20260909T182617729039/summary.json`，北京到上海、2026-09-12出发、酒店入住至09-14，`--details 3`。47次调用，45次成功、2次供应商业务失败；真实错误未隐藏，退出码为1。

- 航班默认搜索10条，三条详情分别返回52、30、30个舱位报价。机场、航站楼、舱位、税费、机型、时长、准点率、退改、行李、餐食等已实际取得；部分航班出发航站楼仍为空。
- 火车两页共55条，完成供应商报告的两页；详情返回完整列车信息、席别、价格、余票、售卖状态、资源ID。没有再将全部席别折成一个最低价。
- 酒店两页共10家，完成供应商报告的两页；三家详情分别返回6、11、7个房型及其报价列表，包含日期政策、餐食、取消说明、库存、报价ID和 `preBookParam`。地址在搜索中有值，部分详情不返回地址，不覆盖掉搜索证据。`rmbPrices` 的单晚/整段口径仍不能仅凭字段名确定，保留原值。
- 地点检索取得地址、坐标、POI ID、部分入口坐标及扩展内容；驾车取得出租车估算、费用、耗时、导航动作、道路坐标串、路况和行政区域信息。示例三条v5路线的出租车估价分别为16、18、142元，仅代表本次供应商估计。
- `NEAR_GO`、`NEAR_BACK` 返回 `errorCode=170003`、`success=false`；`TRANSFER` 在该城市/日期样本返回空列表。无法据此承诺这些模式有可用候选。

文档与实测存在结构差异，例如v5路线的区县字段可能实际名为 `districts`，公开表写作 `district`；完整解析保留真实字段，覆盖报告同时列出文档字段未出现和额外字段出现，不静默改写。其他条件字段（共享航班、某些席别、出口坐标、收费道路等）仍需合适样本才能验证非空。

验证包括固定精度/嵌套字段断言、模拟HTTP、SSE分块、损坏JSON、错误ID、超时残片和Unicode转义密钥。独立对抗审查发现的三个问题已经加入回归；把Decimal解析故意替换成普通float解析后，金额精度断言如预期失败。正常测试通过，不以“全字段解析成功”替代供应商数据质量或业务可行性保证。

## 旧 DTO 实验（保留对照）

以下说明针对旧 `run.py`、`models.py`、`tuniu.py`、`amap.py`，不是上面的全字段入口。旧入口仍指向已删除的 `policy.md`，因此现在运行旧入口的 `all/policy` 会失败；本轮遵照要求不修改政策工具、文件或旧入口。旧政策的成功记录只代表当时样本。

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
