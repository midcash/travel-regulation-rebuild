# GitHub #7：机场到酒店、酒店到会场两段路线

本工单把 `calculate_route` 接入真实工具主链路。首选参数是本次运行中 `resolve_place` 产生的地点候选 ID，工具从候选保存的 GCJ-02 坐标查询高德驾车路线；为兼容实验二的坐标参数形状，直接坐标只有在与唯一已观察候选完全一致时才接受。允许的两类连接是“到达地点 → 酒店”和“酒店 → 会议地点”；已有第一段后，第二段必须复用同一个酒店候选。每次调用保留独立 HTTP 证据、调用 ID 和本地唯一路线 ID，即使供应商返回相同的路线编号也不会互相覆盖。

路线返回的 `duration_seconds` 带有 `duration_basis=query_time_estimate`。`toll_cny`、`taxi_fare_cny` 和供应商原始 `supplier_cost` 分开保存；缺失费用为 `null`，不会补零，通行费不会被当作出租车价格。地点仍可能是供应商排序候选，`identity_confirmed` 不会被路线工具擅自改成已确认。资料不足或路线失败时，主链路正常结束为 `needs_review/missing_data`，不回退演示数据。

## 离线验收

```powershell
D:/anaconda/python.exe -m unittest discover -s tests -p test_route_contract.py -v
```

当前工单合同测试共 3 项：主链路两段路线与独立证据、错误角色/未观察地点拒绝、HIL 选择酒店后暂停恢复仍使用所选酒店。测试在真实工具边界注入固定 HTTP 响应，供应商请求仍经过主链路的 HTTP、证据和计数逻辑，不导入实验包。

类型检查与编译：

```powershell
.venv/Scripts/python.exe -m mypy travel_agent/route.py travel_agent/real_tools.py travel_agent/human.py travel_agent/agent.py --follow-imports=silent --ignore-missing-imports
D:/anaconda/python.exe -m compileall -q travel_agent
```

既有地点、交通详情、住宿、执行快照和 HIL 合同测试继续通过。工作区全套测试中仍有 5 项演示评测因用户预先把场景会议日期从 2026-09-12 改为 2026-11-12 而失败；本工单没有覆盖该人工改动。

## HIL 复现

`tests/test_route_contract.py::RouteContractTests::test_hil_selected_hotel_is_used_by_both_routes_after_resume` 先查询多个酒店候选，保存人工选择并退出；随后用新的 `Store` 实例恢复运行。最终 Trace 的两条路线分别把所选酒店记录为第一段终点和第二段起点，且没有重复模型调用或覆盖路线证据。

线上联调需要主项目 `.env` 中的 `DEEPSEEK_API_KEY`、`TUNIU_API_KEY` 和 `AMAP_API_KEY`，本次环境未提供这些凭证，因此未宣称线上 Trace 验收通过。线上运行入口仍是现有 `plan_trip(..., tool_mode="real")` 或 HIL 的 `start_trip/respond_trip/resume_trip`，不需要实验目录配置。
