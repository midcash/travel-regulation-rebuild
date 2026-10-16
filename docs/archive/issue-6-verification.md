> 历史归档：正文保留原阶段的描述和验收结论，不是当前实现、开发要求或本次验证结果。当前说明见 [开发说明](../development.md)。本次仅调整归档标识及失效引用。

# GitHub #6：机场、酒店和会议地点解析

本工单把地点解析接入主链路的 `resolve_place` 工具。到达机场或车站必须来自本次运行中观察到的交通候选；酒店必须来自本次运行中观察到的住宿候选；会场使用原始请求。查询会保留候选名称、地址、坐标、城市、来源候选、来源证据和供应商原始字段。地点坐标按高德返回的 GCJ-02 保存，详情或地理编码结果只作独立补充，不覆盖主候选坐标。

供应商排序不等于人工确认。名称不完全匹配、城市不一致、坐标无效、返回多个候选、地址不一致、详情或地理编码与主坐标冲突，都会保留 `uncertainty_reasons` 并将 `match_confidence` 设为 `uncertain`；`identity_confirmed` 始终为 `false`。重复查询使用本次调用的独立 `call_id` 和证据文件，不覆盖旧记录。缺少来源或修改会场城市、关键字会在发 HTTP 前拒绝。

## 离线验收

```powershell
D:/anaconda/python.exe -m unittest discover -s tests -p test_place_contract.py
```

当前提交范围的地点合同测试共 12 项，覆盖会场查询、交通及酒店来源绑定、HIL 恢复、候选选择、重复证据、供应商失败、超时重试、秘密脱敏、无效坐标、名称后缀、详情机场冲突和地址补充。`mypy` 对 `places.py`、`real_tools.py`、`human.py`、`agent.py`、`settings.py` 通过，主链路包编译通过。

提交范围全套回归使用临时隔离副本运行，恢复开发前演示日期及模型预算，排除用户预先留下的 `tests/test_transport_detail.py`；结果为 100 项全部通过。当前工作区全套测试的 5 项失败来自用户已有的演示日期从 9 月改为 11 月，未修改这些用户变更。

独立安装验证：构建 wheel 后只复制主包和地点合同测试到临时目录，确认不存在 `experiments` 目录，12 项地点测试全部通过。变异验证把“`identity_confirmed` 必须为 `false`”改成相反断言，测试按预期失败。

## 真实主链路复现

真实 DeepSeek、高德和途牛联调脚本位于 `artifacts/place-acceptance/live.py`，需要主项目 `.env` 的 `DEEPSEEK_API_KEY`、`TUNIU_API_KEY` 和 `AMAP_API_KEY`。脚本通过现有 `plan_trip` 执行，不导入实验包：

```powershell
$env:PYTHONPATH = (Get-Location).Path
D:/anaconda/python.exe -X utf8 artifacts/place-acceptance/live.py
```

本次 Trace 为 `310c71d36a444aedbfb80a1f70a5720b`，结果为 `completed / needs_review`。模型自主调用政策、交通搜索、住宿搜索、交通详情及三个 `resolve_place` 查询；共 8 次模型调用、12 次 HTTP、0 次重试。到达机场、酒店和会场均返回真实候选，因各查询存在多个 POI，均保留未确认/不确定状态。原始响应文件位于 `artifacts/place-acceptance/evidence/<trace_id>/`，结构化摘要位于 `docs/issue-6-live-summary.json`。

本阶段没有路线工具，因此即使三个地点均已查询，正常结束仍为 `needs_review/missing_data`，不会宣称完整方案可行，也不会用模拟数据回退真实失败。
