# Pi 迁移前回归基线

2026-10-16，现场 `0b7364f`，干净工作区。该提交已包含此前上限扩大与场景 11 月日期，不能再称为未提交修改。

本轮选择撤回未经实测的扩大：旧循环恢复 10 次模型、12 次工具、24,000 token；HIL 默认 3 次人工请求。只修改对应默认值，不回退整个提交。Pi 第 0 步使用独立验证限额，不继承或声称这些值适合 Pi。

固定回归日期恢复 2026-09-12；既有 `evals/cases.json` 独立预期不变。HTTP 集成测试固定时钟 2026-09-10 09:00+08:00；自然语言日期测试保持原有 2030 固定时钟。页面示例由服务端实际上海时间加三天生成，产生解析器能够处理的明确日期；不把演示夹具推迟到未来来规避日期检查。

验证环境：Windows，`D:/anaconda/python.exe` 3.11.11，Node 22.20.0，httpx 0.28.1。本机旧 `.venv` 的 Python312 路径失效，本轮不把它作为已可用环境。

首次全量 131 项测试出现 5 个日期相关失败；修复后增加页面示例与解析测试，共 132 项通过。三项 Node UI 测试通过。baseline eval 六个固定案例均需通过；该验证只证明旧演示合同，不证明 DeepSeek 或真实供应商质量。

复现命令：

```powershell
& 'D:/anaconda/python.exe' -B -m unittest discover -s tests
node tests/agent_workspace_ui.test.cjs
node tests/hil_ui.test.cjs
node tests/request_ui.test.cjs
& 'D:/anaconda/python.exe' -X utf8 -m travel_agent eval --provider baseline
```

本文件与基线修复同一次本地提交；准确提交 SHA 写入第 0 步最终报告。所有本地提交在验证和报告完成后统一 push。
