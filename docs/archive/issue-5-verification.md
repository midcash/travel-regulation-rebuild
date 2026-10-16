> 历史归档：正文保留原阶段的描述和验收结论，不是当前实现、开发要求或本次验证结果。当前说明见 [开发说明](../development.md)。本次仅调整归档标识及失效引用。

# GitHub #5：住宿候选与房型详情验收

2026-09-12。开发/审查基点：`fc72a9cc99b31dc4ae7010d97a0e65df6f3313a3`。

新增 `search_stays(destination, check_in, check_out, meeting_place?, max_results=5)` 和 `get_stay_detail(candidate_id)`。搜索最多展示20个候选，详情保留供应商返回的全部房型和报价字段；HTTP响应仍受现有16MiB及45秒单次上限约束。默认1成人、1间房，在结果及提示词说明。会议地点仅作为搜索关键词，不表示已核验距离或地点。

酒店详情只接受本次成功搜索中的候选ID，日期、人数及酒店ID来自原查询。数字形式的供应商ID转换为整数，非数字ID保留字符串；缺少ID不发HTTP，外部/未观察ID触发既有guardrail。详情返回明确酒店ID或日期冲突、房型/报价结构错误时，保留失败证据，不生成替代详情。搜索查询偏离原目的地或一晚住宿日期时标记 `meets_request=false`，保留原条件。

搜索保留酒店地址和原始供应商字段；详情保留自身返回的字段，并用 `candidate_id/candidate_evidence_ref` 关联搜索。详情缺地址时不补造，可通过搜索证据读取地址。金额沿用十进制字符串序列化，未知值不补零；`lowest_price_raw` 和房型报价均标明 `price_basis=unknown`，不计算整段总价。入住/退房日期是查询条件，供应商入住政策与未来规划的办理时间分开。

复用主链路HTTP、脱敏、证据、重试计数与预算，不导入实验包，不读取实验配置/历史结果，不修改实验二。沿用 `transport_http` 注入点及 `transport-*.json` 证据文件命名；该既有传输边界现也服务住宿，未新增框架或配置层。工具由模型自主选择，失败不回退模拟数据。当前仍缺地点和路线，结束只允许 `needs_review/missing_data`。

## 真实主链路复现

真实 DeepSeek/途牛 Trace：`3bc3968061334a24b105c3f017ee15a6`。模型自主调用政策、交通搜索、住宿搜索、交通详情、住宿详情后正常结束：`completed / needs_review`，6次模型调用、5次业务工具调用、4次HTTP、0次重试。输入Token58690，输出647。本次显式配置 `Limits(max_tokens=100000, timeout_s=240)`，其他限制默认；该配置不是完整后续链路的预算保证。

住宿搜索日期2026-09-18至2026-09-19，返回8个候选。模型查询第8个候选“三亚湾红树林度假世界（木棉酒店）”，供应商ID `200203865`，搜索地址“三亚湾凤凰路155号（近三亚火车站）”；详情12种房型。首个房型原始报价 `290.0`，口径未知；详情政策14:00入住、12:00退房，不作为本次实际办理时间。详情没有地址，搜索证据仍可追溯。可提交的业务摘要见 [issue-5-live-summary.json](issue-5-live-summary.json)，不包含预订参数和私有模型消息。

在项目根目录保存以下脚本，并使用 `python -X utf8 <脚本路径>` 运行；需要主项目 `.env` 中的 `DEEPSEEK_API_KEY` 和 `TUNIU_API_KEY`。日期过期后改为未来日期，不要求返回同一酒店。

```python
import json
from pathlib import Path
from travel_agent.agent import Limits, plan_trip
from travel_agent.providers import make_provider
from travel_agent.settings import load_env
from travel_agent.telemetry import Store

load_env()
folder = Path("artifacts/stay-acceptance")
folder.mkdir(parents=True, exist_ok=True)
request = {"origin": "杭州", "destination": "三亚",
           "meeting_location": "三亚湾红树林国际会议中心",
           "meeting_at": "2026-09-19T09:00+08:00",
           "transport_mode": "flight", "arrive_previous_day": True}
run = plan_trip({"id": "stay-live", "request": request}, make_provider("deepseek"),
                Store(folder / "trace.sqlite3"), tool_mode="real",
                limits=Limits(max_tokens=100000, timeout_s=240))
(folder / "run.json").write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
print(run["trace_id"], run["termination"])
```

本地完整结果 `artifacts/stay-acceptance/run.json`，数据库 `trace.sqlite3`，原始证据 `evidence/<trace_id>/transport-*.json`，均位于同目录。查看主链路Trace或页面：

```powershell
python -X utf8 -m travel_agent --db artifacts/stay-acceptance/trace.sqlite3 trace 3bc3968061334a24b105c3f017ee15a6
python -X utf8 -m travel_agent --db artifacts/stay-acceptance/trace.sqlite3 serve --provider deepseek
```

展开工具结果与证据，核对搜索日期、候选地址、详情关联和房型报价。模型可因资料不足提前结束；仅正常结束不等于住宿详情验收成功，应同时检查两项住宿工具的 `status=ok` 和实际字段。

## HIL 与后续兼容

住宿复用既有候选选择暂停点，不新增工具内部暂停或重试节点。交通与住宿的详情约束按搜索来源区分；跨任务ID、违背同类型已确认选择的详情仍拒绝。联合测试通过 `start_trip`、重建Store、`resume_trip/respond_trip`，选择第二航班再选择第二酒店，确认四次HTTP对应正确对象，重复答复不重放请求。模型忽略酒店选择的反例在HTTP之前拦截。

快照字段仍为现有 `history/seen/_transport_attempts` 等；新增工具、提示词及HIL schema描述会改变协议指纹，旧版本未完成HIL任务依兼容约定C13拒绝静默恢复，升级前应处理完或取消，再新建任务。此次human schema说明变化也涉及demo交互指纹；demo非HIL执行和已完成Trace读取不变。未实现旧快照迁移。

后续#6地点解析应结合酒店候选地址和详情证据，明确来源；不得因详情缺地址而丢失已观察搜索地址，也不得把关键词当成准确地点。房型选择、预订、完整行程校验和自然语言入口没有在本工单扩展。完整链路预算仍在后续联调工单验证。

## 验证与提交边界

新增8项 `tests/test_stay_contract.py` 公共接口测试，覆盖日期、ID、精度、错误、重试、跨领域HIL及恶意忽略选择；首条主链路、HIL联合和日期约束断言均先失败再实现。报价口径断言变异为 `stay_total` 后按预期失败，正式断言保持 `unknown`。隔离wheel中不含实验目录，8项住宿测试通过。

工作区直接运行 `python -m unittest discover -s tests`：103次执行，5项失败，均由用户将演示日期改为11月、既有evals/测试仍期待9月引起。在临时副本中保留本工单全部改动，仅使用HEAD中的演示数据及原默认预算，103次执行全部通过（33.867秒）；包含未跟踪测试贡献的15次执行，本次提交范围对应88项测试。工作区数据未被还原或修改。

`.venv/Scripts/python.exe -m mypy travel_agent/stays.py travel_agent/real_tools.py travel_agent/transport.py travel_agent/human.py travel_agent/agent.py --follow-imports=silent --ignore-missing-imports`：5文件通过；`compileall`、diff空白检查通过。wheel SHA-256：`d1084ec9bf2387af5e66bd9c58fbd767601fe297494c668eab1c24ead3c586c5`。wheel按实际工作区构建，包含用户已有默认预算和演示日期；隔离全量验证则专门按本次提交范围构建临时源码副本，两者不可混为一项测试。

独立Standards审查无规范/设计阻断项，指出原有 `/api/config` 断言位于return之后未执行；已移出fetch函数并在全量测试验证。独立Spec审查核对工单、住宿/HIL反例及真实Trace，无剩余阻断项。

本工单保留用户先前对 `Limits.max_tokens`、演示日期及未跟踪 `tests/test_transport_detail.py` 的修改，不纳入提交。该未跟踪测试导入旧测试类，完整discovery会有14次重复交通测试执行。
