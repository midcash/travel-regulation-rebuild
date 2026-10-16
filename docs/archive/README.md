# 历史验证归档

这里保留历史阶段的验收记录、运行摘要和调研快照，供追溯使用。正文中的“当前”“尚未实现”“用户最新要求”均指原记录的上下文，不是今天的开发指令。旧测试数量、模型结果和远程工单状态不代表当前工作区重新验证通过。

当前开发入口为 [README](../../README.md)、[开发说明](../development.md)、[工具合同](../tool-contracts.md)和[人工交互接口](../hil-usage.md)。

## 阶段记录

- [第一阶段验证](verification.md)：固定演示、真实模型及旧页面的历史验证。
- [HIL 分阶段实施](hil-implementation-progress.md)：执行内核、持久化和旧页面交互的历史验收。
- [政策接入](issue-2-verification.md)、[交通详情](issue-4-verification.md)、[住宿](issue-5-verification.md)、[地点](issue-6-verification.md)：各阶段局部验证，不代表当时具备完整真实行程。
- [两段路线](issue-7-verification.md)、[完整行程](issue-8-verification.md)、[自然语言入口](issue-9-verification.md)：注明了固定响应与线上联调的证据边界。

对应的 issue-2/4/5/6-live-summary.json 随记录保留。它们是历史业务摘要，不是当前可售商品，也不替代完整原始响应。记录中引用的 artifacts 和实验 results 是本机产物，可能不随仓库提供；有链接或历史 Trace ID 不等于能在新环境重现。

## 调研快照

- [Issue #10 调研](research/issue-10.md)：2026-10-13 的工单要求和状态。
- [GitHub 状态核对](research/project-status-2026-10-14.md)：2026-10-14 的远程快照，未在本次清理中重新查询。

## 旧规格的处理

2026-10-16 按用户裁决撤下第一阶段验收约定、旧问题清单、真实工具接入规格和 HIL 兼容约定。有效的现有技术约束已提取至当前开发文档；阶段性业务设想、旧页面/循环保护要求不再作为开发规范。原文可用 `git log -- docs/<旧文件名>` 及 `git show <历史提交>:docs/<旧文件名>` 追溯，不修改历史验收事实。

本次清理未重新运行历史业务验收，也未变更代码、政策资源或远程工单。
