# GitHub #4：交通详情接入验收

2026-09-12。审查基点：`a4b2ee61383d293e87e106a64fb47bb09840ff15`。

本阶段新增真实工具 `get_transport_detail(candidate_id, max_options=20)`，支持飞机和火车，最多返回 20 项舱位/席别。候选必须来自本次 Trace 中成功的 `search_transport`；城市、日期、班次及站名从已有历史读取，模型不能重新指定。飞机调用 `multiCabinDetails`，火车调用 `queryTrainDetail`。运行时不导入实验代码、不读取实验配置或历史结果。

详情在自己的调用 ID 下保存，`candidate_id`、`candidate_evidence_ref` 保留原候选关系。原始班次对象和迁入版本的报价字段保留；JSON 小数以十进制字符串传递，不做浮点金额转换，未知价格不补零。返回班次、明确日期或火车站点与原查询冲突时，详情返回错误，不挂接虚假数据。没有返回的机场或航站楼不从候选补造；机场名称全称/简称不作无依据的强制等同或冲突判断。

无效、跨任务、未观察候选及多余参数触发原有 `invalid_response`/guardrail 路径；候选缺少班次或站名返回 `MissingCandidateData`，不发送 HTTP。供应商失败沿用结构化错误和脱敏证据；重试由模型再次调用工具发起，HTTP 层不自动重试。详情与搜索分别计算重试关联，HTTP 次数、工具次数和 retries 均记录在 Trace。

## 真实验证与复现

输入：杭州出发、飞机、2026-09-19 09:00 在三亚湾红树林国际会议中心开会，提前一晚抵达。真实 DeepSeek 模型自主进行了政策读取、交通搜索、详情查询和结束调用，未使用固定工具调度。

成功 Trace：`43dfd1383f8a4c828136a64e8444bc09`。4 次模型调用、3 次业务工具调用、2 次交通 HTTP 请求、0 次重试。详情返回 CZ6630：2026-09-18 18:20 萧山 T4 出发，21:40 凤凰 T1 到达，10 项舱位报价。结束为 `completed / needs_review / missing_data`；该阶段尚无酒店和路线证据，不宣称完整方案可行。实际字段见 [issue-4-live-summary.json](issue-4-live-summary.json)，线上商品和报价具有查询时效性。

首次真实执行已取得详情，但在后续模型响应达到默认 24,000 Token 预算，Trace 为 `6a8cc11f54734a76b11d8e3c80d7c8c7`，终止原因 `budget_exceeded/tokens`。成功复验使用现有 Python `Limits(max_tokens=60000)`，其余限制保持默认；生产默认值未调整。后续 #10 仍需按完整链路设置和验证预算，不能把本次配置视为全链路保证。Windows 控制台请使用 `-X utf8`，避免政策字符导致 GBK 打印失败。

在项目根目录将以下内容保存为本地脚本，以 `python -X utf8 <脚本路径>` 运行。需要主项目 `.env` 或环境变量中的 `DEEPSEEK_API_KEY`、`TUNIU_API_KEY`，不使用实验配置：

```python
import json
from pathlib import Path
from travel_agent.agent import Limits, plan_trip
from travel_agent.providers import make_provider
from travel_agent.settings import load_env
from travel_agent.telemetry import Store

load_env()
folder = Path("artifacts/transport-detail-acceptance")
folder.mkdir(parents=True, exist_ok=True)
request = {
    "origin": "杭州", "destination": "三亚",
    "meeting_location": "三亚湾红树林国际会议中心",
    "meeting_at": "2026-09-19T09:00+08:00",
    "transport_mode": "flight", "arrive_previous_day": True,
}
run = plan_trip({"id": "transport-detail-live", "request": request},
                make_provider("deepseek"), Store(folder / "trace.sqlite3"),
                tool_mode="real", limits=Limits(max_tokens=60000))
(folder / "run.json").write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
print(run["trace_id"], run["termination"])
```

日期过期后应更换为未来会议日期。模型可能选择不同候选或因资料不足提前结束；此时不能当作详情成功验收，应核对实际 `tool_results`。

完整本地 Trace 数据库：`artifacts/transport-detail-acceptance/trace.sqlite3`；原始证据：同目录 `evidence/<trace_id>/transport-*.json`；结果：`run.json`。可通过现有 `trace` 命令或页面查看：

```powershell
python -X utf8 -m travel_agent --db artifacts/transport-detail-acceptance/trace.sqlite3 trace 43dfd1383f8a4c828136a64e8444bc09
python -X utf8 -m travel_agent --db artifacts/transport-detail-acceptance/trace.sqlite3 serve --provider deepseek
```

页面选择对应记录，展开“工具结果与证据”中的 `get_transport_detail`，查看 `flight.arrivalAirport/arrivalTerminal` 和候选证据关系；公开结果由原有 Trace 接口提供。本工单未改页面布局。

## HIL 与后续工单

沿用既有候选选择交互和 `candidate_id` 执行约束，无新增暂停节点。新测试通过 `start_trip` 暂停、重新构建 Store、`resume_trip/respond_trip` 恢复，验证选择第二个候选后供应商收到第二个班次，模型改查第一个则被拦截。同答复重放不重复查询。详情结果、重试记录都使用现有 `history/seen/_transport_attempts` 快照字段，无新增持久化字段。

新增工具及提示词会改变 real 模式的 schema/prompt 指纹。按照兼容约定 C13，升级前尚未完成的 real HIL 任务不能用新协议静默恢复；应在旧版本处理完或取消后新建任务。已完成 Trace 可继续读取，demo 协议不变。本工单未引入旧快照迁移器。

后续住宿工具应按自身候选类型扩展 HIL 选择约束，避免把交通 candidate_id 约束直接套到酒店。地点/路线工单使用详情的独立证据和原始机场字段，不能将航站楼缺失当作已确定地点。完整到会判断及自然语言输入仍属于原后续工单。

## 验证记录

- 新增 `tests/test_detail_contract.py` 9 项公共接口测试：候选关联、报价精度、无效候选、供应商错配/缺字段、火车方式、重试预算和 HIL 恢复。
- 原有未提交 `tests/test_transport_detail.py` 保持原样，参与本地验证但不纳入本次提交。该文件导入旧测试类，完整 discovery 会重复执行 14 项交通搜索测试。
- `python -m unittest discover -s tests`：95 次测试执行全部通过，包含上述重复的 14 次；本次提交范围对应 80 项测试，另有保留的未提交详情测试 1 项。
- `.venv/Scripts/python.exe -m mypy travel_agent/transport.py travel_agent/transport_detail.py travel_agent/real_tools.py travel_agent/agent.py --follow-imports=silent --ignore-missing-imports`：4 文件通过；现有项目未完全标注类型，不代表严格静态类型证明。
- 变异：将详情到达航站楼改为 `WRONG`，公共接口断言按预期失败；正式代码未保留变异。火车错误日期和站点的反例先失败、修复后通过。
- wheel 构建并解包至无实验目录的临时环境，验证实际从安装包加载，9 项新测试全部通过。wheel SHA-256：`f57384eecd82c8624a57697911d2018f897b3650d43f7552ccab98e6af6d23b9`。
- 独立 Standards 审查：0 项。独立 Spec 审查发现 1 项火车日期/站点关联遗漏，修复并复核通过，无剩余阻断项。

本地 artifacts 不随 Git 提交；工单未关闭，远程仓库未推送。
