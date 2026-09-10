# Issue #2 政策工具接入验收

本阶段通过现有 `plan_trip` 接入独立政策工具，真实模式仅开放 `get_policy_snapshot` 和 `finish`。模型按需选择调用，允许重复查询或直接以资料不足结束。政策读取不等于合规认证，不能仅凭政策宣称完整行程可行或市场无方案。既有演示模式仍为默认值，历史评测保持其原有定位；真实模式必须显式选择，不会在失败后回退演示。

## 复现

从项目根目录执行（Windows 使用 UTF-8，避免政策原文字符超出 GBK）：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m travel_agent --db artifacts/issue-2-live.sqlite3 run --provider deepseek --tool-mode real --scenario hangzhou-shanghai
```

模型配置使用主项目 `.env` / 环境变量中的 `DEEPSEEK_API_KEY` 等现有配置；不加载实验配置。输入为杭州到上海，会议地点东方明珠塔，会议时间 `2026-09-12T08:00+08:00`（可核对随附 JSON 中实际输入）。该历史结构化场景只复用请求字段，真实模式不加载其模拟记录。

API 同样支持在现有 `/api/run` JSON 中加入 `"tool_mode": "real"`。Python 调用为 `plan_trip(scenario, provider, store, tool_mode="real")`。`policy_path` 是 Python 调用方可选的受控文件配置，不向模型开放路径参数；默认读取随包副本。

预期：政策调用成功，结果带状态、来源、原文字节哈希及证据引用；最终 `termination=completed`、`status=needs_review`、`reason=missing_data`，行程为空、总价未知。模型可以不查询而直接结束，线上复现不保证固定调用序列。

实际：2026-09-10 真实 DeepSeek 运行进行了 2 次模型调用、1 次政策调用，并得到上述预期结束结果。Trace ID 为 `554c3c1015b246009e38d978f486bc89`。首次控制台打印遇到 GBK 编码错误，但运行和 Trace 持久化均已完成，随后使用 UTF-8 成功导出。关键运行字段保存在 [issue-2-live-summary.json](issue-2-live-summary.json)。此结果仅验收政策阶段，不代表完整真实差旅行程已经实现。

本地完整记录：`artifacts/issue-2-live.sqlite3`；UTF-8 导出：`artifacts/issue-2-live-trace.json`。既有 Trace 查看入口：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m travel_agent --db artifacts/issue-2-live.sqlite3 trace 554c3c1015b246009e38d978f486bc89
```

Trace 的 `tool_results` 保留调用结果，`evidence` 保留政策快照，`execute_tool` span 保留调用 ID、工具状态、来源与哈希。重复查询以 Trace ID 加调用序号生成独立证据标识。缺失、不可读、损坏文件返回 `PolicyReadError` 和 `retryable=false`，没有替代文件或模拟回退。

## 验证记录

- 实验原文与迁入副本逐字节相同，SHA-256：`14e89ab8c38fa908dd9652cff0ba5310dd491f6d070736c70a27ce418555c49d`。Git 属性关闭该副本的换行转换；实验二源码及资源未修改。
- `python -m unittest discover -s tests -v`：20 项通过，包括既有 HTTP、OTLP、评测回归及 6 项政策测试。
- `python -m mypy travel_agent/agent.py travel_agent/real_tools.py travel_agent/regulation_policy.py travel_agent/domain.py travel_agent/cli.py travel_agent/server.py --follow-imports=silent --ignore-missing-imports`：6 个文件无错误。沿用当前未全面标注类型的代码，不把此结果当成严格类型保证。
- wheel 构建成功；仅解包 wheel 与政策测试到临时目录，用 `python -I -S` 禁用 site-packages 执行 6 项测试，全部通过。隔离包不含实验目录，证据读取来源在隔离包中；记录为 `artifacts/issue-2-isolation.txt`。
- 将隔离测试的预期哈希改为全零，政策来源断言按预期失败；记录为 `artifacts/issue-2-mutation.txt`。生产文件及正式测试未保留变异。
- code-review 独立审查：Standards 0 项；Spec 0 项。审查基点为 `b85d9cb3ef6e41c0af7ff8181b625f5ac6b55959`，仅审查本工单改动。

本地 artifacts 不随 Git 提交；可用上述命令生成新 Trace。线上调用使用真实模型和随包政策文件，本阶段不需要供应商交通 API。
