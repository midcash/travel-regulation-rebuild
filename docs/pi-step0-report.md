# Pi 第 0 步：保留结论与复现入口

原实测日期 2026-10-16；本记录于 2026-10-18 精简。它证明接入前提，不表示正式会话、真实供应商或页面产品验收通过。当前代码与验证边界见 [开发说明](development.md)，实施决定见 [Pi 方案](pi-migration-plan.md)。

## 可复用的实测结论

Pi SDK 1.3.0 支持从外部 SQLite entries 重建内存 AgentSession，保留工具消息、压缩记录和恢复所需推理结构，无须 Pi 会话文件。Windows 上由 Python 管理 Node 子进程可行；DeepSeek 工具调用、流式文本/推理、缓存 usage、steer、取消、压缩和恢复曾用真实模型验证。

隔离采用临时 cwd、重定向配置目录、内存设置/凭证/模型注册与资源加载器；探针观察及主动拒绝项目指令/真实 home 访问。它是行为回归检测，不是任意恶意代码系统沙箱，正式 runtime 仍须补齐 Pi 方案中的隔离边界。

工具结果先提交 SQLite 后交 Node；完成结果重复请求不重复调用，相同 ID 不同参数拒绝；提交前中断保留 unknown，继续查询使用新 ID。选定状态与摘要分别保存，空闲选定不调用模型，恢复不自动续费。

Pi input 不含 cacheRead，总输入须两者相加；reasoning 已在 output 内。取消或未收到完整 usage 时标记不完整，SDK 的零值不代表免费。历史压缩曾因 4096 输出上限不完整，验证为压缩单独使用 8192；这些是探针参数，不是正式模型规格或产品预算。

2026-10-18 新增可移植回归发现工具等待时取消仍可能进入下一次模型请求；worker 现已在取消/关闭后禁止新模型请求，同时允许用户显式开始下一轮或压缩。这个修复尚未做新的付费实测。

## 保留代码

`verification/pi-step0/` 的 lockfile、worker、Python control、离线/真实模型驱动和本地 OTLP 接收器仍可复用作参考；不是正式 runtime。默认验证改为 `test_portable.py`，不依赖旧 Agent/HIL、Git 历史源码或未提交的本机夹具。复现命令见[目录说明](../verification/pi-step0/README.md)。

旧 replay/report 驱动已删除，未把私有历史数据搬进测试目录；新工具测试使用明确标注的合成 HTTP 响应。历史十二次回放累计 394607 token、104 次到达 Python 的工具请求，其中 14 次无历史数据；不能把正常结束次数当成完整方案成功次数，也不能用这些数据推断当前成本。

## 历史证据位置

清理前完整文档与驱动在 `codex/pre-skeleton-cleanup-20261018`（`30619f0dc5debbb64a85a6c21c7d4795a4baaecf`）；更早完整原始结果可用 `git show legacy-pre-pi:docs/pi-step0-results.json` 查询。现场验证提交为 `0b7364f02e9f89ea08e42929becc8b9cc41f88fb`，验证代码封存提交为 `5e74767`，历史报告可通过 Git 追溯。

本机 `artifacts/pi-step0/` 的历史 Trace、结果与密钥未读取、迁移或删除；它们未纳入仓库，Git 恢复不覆盖这类本机数据。旧 HIL 仅测试数据，不做新历史投影，也不修改旧表。
