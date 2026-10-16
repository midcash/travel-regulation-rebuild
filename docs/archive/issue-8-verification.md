> 历史归档：正文保留原阶段的描述和验收结论，不是当前实现、开发要求或本次验证结果。当前说明见 [开发说明](../development.md)。本次仅调整归档标识及失效引用。

# GitHub #8：完整真实行程与准时到会验收

真实模式现在接受并校验四段结构化方案：供应商交通候选、到达地至酒店路线、酒店候选、酒店至会场路线。模型单独提供计划离店时刻与到达缓冲；应用按观察到的路线时长计算到会时间，并检查地点连接、前一日抵达、一晚住宿、证据引用和缓冲后的会议截止时间。真实交通与住宿费用仍不合计，缺少杭州出发地址时也不包含杭州住所到机场/车站的接驳。

## 可复现主链路

固定输入：

```json
{
  "origin": "杭州",
  "destination": "三亚",
  "meeting_location": "三亚湾红树林国际会议中心",
  "meeting_at": "2099-09-19T09:00+08:00",
  "transport_mode": "flight",
  "arrive_previous_day": true
}
```

离线执行现有 `plan_trip(..., tool_mode="real")`，模型和供应商分别使用固定测试响应；供应商请求仍经过主链路 HTTP、证据记录、预算与 Trace 逻辑。PowerShell 中可将 Trace 保留在忽略目录，然后运行：

```powershell
$env:TRAVEL_ISSUE_8_TRACE_DIR = ".tmp/issue-8"
python -m unittest discover -s tests -p test_real_itinerary.py -v
```

成功场景的 SQLite Trace 位于 `.tmp/issue-8/complete-real-itinerary.sqlite3`，测试输出包含 `trace_id`；原始固定响应证据位于 `.tmp/issue-8/evidence/<trace_id>/`。不设置该环境变量时，测试使用临时目录并在结束后清理。

## 预期与实际

固定样例的交通供应商时刻保持为 9 月 18 日 16:05 起飞、18:55 抵达；到达路线估算 21 分钟，预计 19:16 到酒店；住宿查询日期为 9 月 18 日至 19 日。次日计划 08:00 离店，酒店到会场路线为 942 秒，预计 08:15:42 到达；加入 30 分钟缓冲后为 08:45:42，早于 09:00 会议。输出保留供应商时刻与规划时刻的区别、逐段证据 ID、未知费用说明和未包含的杭州住所接驳。

当前固定响应验收共 8 项，覆盖完整方案、09:01 迟到拒绝、路线段错接、会议当天抵达、未观察证据、错误的“无可行方案”结论、未人工选择的歧义地点拒绝，以及 HIL 选择会场后恢复并继续完成方案。失败结果均经 `plan_trip` 或 `start_trip/respond_trip` 公共接口观察。迟到判断的变异验证曾将比较条件反转；09:01 用例随之错误地得到 `completed/planned` 并失败，恢复正确条件后该用例通过。

适用回归还包括 `test_route_contract.py`、`test_transport.py`、`test_detail_contract.py` 和 `test_hil.py`。这些离线固定响应结果不代表真实模型或供应商线上联调；真实 API 到最终页面的联调仍按父规格由后续工单承担。
