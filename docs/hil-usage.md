# 当前人工交互接口

本文描述当前 Python HIL，不是 Pi 会话设计。网页默认勾选“需要时询问我”，先确认核心需求；等待时可以选择候选、补充偏好或取消任务。刷新页面或重启同一数据库的服务后，从原运行记录继续回复。取消勾选后使用非交互入口；plan_trip、CLI 和评测默认不启用 HIL。

当前支持同机多进程恢复；人工配额及等待期限通过 Python start_trip 的 max_human_requests/human_ttl_minutes 设置。已提交版本与工作区配额差异见[开发说明](development.md#预算与回归基线)。核心城市、会议地点文本、日期、交通方式或住宿要求修改仍要求新建任务：这是待替换的现有限制，不是后续持续交互产品的要求。答复和恢复不重置执行预算。

## Python

```python
from travel_agent.hil import start_trip, get_trip, respond_trip
from travel_agent.providers import Baseline
from travel_agent.telemetry import Store
from datetime import datetime, timedelta
from travel_agent.domain import CHINA

store = Store("artifacts/observability.sqlite3")
# 未来日期避免示例过期；只使用 demo，不调用供应商。
meeting_day = datetime.now(CHINA).date() + timedelta(days=3)
scenario = {"id": "hil-example", "request": {
    "origin": "杭州", "destination": "上海", "meeting_location": "东方明珠塔",
    "meeting_at": f"{meeting_day}T08:00+08:00"}}
waiting = start_trip(scenario, Baseline(), store)
# 此处可退出进程。新进程重新创建 Store、受控 provider 工厂即可继续。
current = get_trip(waiting["run_id"], store)
result = respond_trip(
    current["run_id"], store, lambda config: Baseline(),
    interaction_id=current["human_request"]["interaction_id"],
    state_version=current["state_version"], response_id="unique-reply-id",
    reply={"confirmed": True, "preference": "酒店安静"},
)
```

示例只适用于返回 confirm 问题且行程日期尚未过期的演示场景。实际调用按 human_request.kind 提交：confirm 用 confirmed 布尔值；preference 用 preference 文本；select 用 option_id，或用 rejected=true 拒绝当前候选。确认拒绝后可继续收集证据，但不能直接返回已确认的可行方案。

恢复工厂接收 provider/model/pricing_usd_per_million/base_url 配置，返回相同模型、端点及计价设置的适配器；密钥与 HTTP 客户端重新从受控运行环境构造，不放进快照。调用 resume_trip 可继续未完成的执行片段；get_trip 只读。cancel_trip 只取消当前版本的等待任务。

## HTTP

创建：POST /api/hil/runs，主体沿用 /api/run 的 provider、tool_mode，以及 scenario、request 或 request_text 输入。

查询：GET /api/hil/runs/{run_id}。

答复：POST /api/hil/runs/{run_id}/respond，主体为 interaction_id、state_version、response_id、reply。resume 接受空 JSON 对象；cancel 接受 state_version。所有写接口均要求原有同源与 X-Travel-Client: dashboard、application/json 规则。

写接口返回执行片段的 job；轮询原 /api/jobs/{id}。job 完成意味着该片段已返回，可能正在等待人；使用 execution_status 判断业务执行状态，再按 run_id 查询。重复提交同一个答复时必须复用 response_id 和相同内容，不能自行生成新 ID 重放。

## 恢复边界

已提交等待和工具结果可恢复；外部调用已发出但结果尚未提交时，不自动重放，终止为 execution_interrupted 并保留不完整用量标记。最终提交失败时维持未完成状态，resume 只补收尾。数据库及同目录锁文件应放在本机文件系统；当前没有跨主机执行协调能力。

私有 hil_executions 表包含恢复所需消息，公开 run/trace 不暴露完整消息或快照。交通、住宿和地点候选约束及两段路线已有实现和离线测试；这不代表真实供应商整链已验收。人工选择绑定已观察候选，模型调用和最终结果均受选择约束；偏好不改写供应商事实。模型配置、提示词、工具协议、数据或政策指纹不兼容时，拒绝静默恢复。

工具语义见[工具合同](tool-contracts.md)；历史测试与实施记录见[归档](archive/README.md)。
