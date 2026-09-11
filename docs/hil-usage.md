# 使用人工交互规划

网页沿用原启动方式，在生成方案前勾选“启用人工交互”。默认先确认核心需求；等待时可以选择候选、补充偏好或取消任务。刷新页面或重启同一数据库的服务后，从原运行记录继续回复。未勾选时，原 plan_trip、CLI 和评测行为保持不变。

首版支持同机多进程恢复。默认最多 3 次人工请求，每次等待 24 小时；通过 Python start_trip 的 max_human_requests/human_ttl_minutes 设置。核心城市、会议地点文本、日期、交通方式或住宿要求需要修改时，新建任务。供应商工具仍按原接入规格演进。

## Python

```python
from travel_agent.hil import start_trip, get_trip, respond_trip
from travel_agent.providers import Baseline
from travel_agent.telemetry import Store
from travel_agent.domain import scenario_by_id

store = Store("artifacts/observability.sqlite3")
waiting = start_trip(scenario_by_id("hangzhou-shanghai"), Baseline(), store)
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

创建：POST /api/hil/runs，主体沿用 /api/run 的 provider、tool_mode、scenario 或 request。

查询：GET /api/hil/runs/{run_id}。

答复：POST /api/hil/runs/{run_id}/respond，主体为 interaction_id、state_version、response_id、reply。resume 接受空 JSON 对象；cancel 接受 state_version。所有写接口均要求原有同源与 X-Travel-Client: dashboard、application/json 规则。

写接口返回执行片段的 job；轮询原 /api/jobs/{id}。job 完成意味着该片段已返回，可能正在等待人；使用 execution_status 判断业务执行状态，再按 run_id 查询。重复提交同一个答复时必须复用 response_id 和相同内容，不能自行生成新 ID 重放。

## 恢复边界

已提交等待和工具结果可恢复；外部调用已发出但结果尚未提交时，不自动重放，终止为 execution_interrupted 并保留不完整用量标记。最终提交失败时维持未完成状态，resume 只补收尾。数据库及同目录锁文件应放在本机文件系统；当前没有跨主机执行协调能力。

私有 hil_executions 表包含恢复所需消息，原公开 run/trace 不暴露这些消息。真实交通候选选择已接入；地点确认与路线的联调依赖后续真实工具工单，不表示这些工具已经实现。

详细约定见 [兼容约定](hil-compatibility-contract.md)，测试与提交阶段见 [实施记录](hil-implementation-progress.md)。
