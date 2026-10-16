# p19 DeepSeekHarness：插件运行时与事件事实源

对应第 19 章。DeepSeekHarness 的学习重点不是背 WebUI，而是两个架构理念：

1. **Everything is a Plugin**：模型、AgentLoop、工具、Session、Sandbox、权限、后台任务和 UI 都可以是遵守服务槽/Registry/生命周期约定的插件。
2. **append-only SessionEventLog**：用户消息、模型输出、工具调用和工具结果只追加不改写，模型消息历史、回放、恢复和审计都从同一事件流派生。

## 原型做了什么

- `CordisRuntime` 管理插件、服务、依赖和 Fiber 状态。
- `Context` 提供服务槽，`Effect` 记录卸载时要清理的副作用。
- `AgentLoopPlugin` 自己也是插件，依赖 `llm` 和 `tools` 服务。
- `SessionEventLog` 写入真实 JSONL，展示“模型可见即已记录”的基本形状。
- `runtime.replace()` 模拟 HMR：卸载依赖 → 清理旧插件 → 加载新插件 → 重新激活 AgentLoop。

## 建议断点

- `plugin_runtime.py:CordisRuntime.load`：静态插件组装。
- `plugin_runtime.py:AgentLoopPlugin.apply`：AgentLoop 如何从服务槽取模型和工具。
- `plugin_runtime.py:SessionEventLog.append`：唯一事实源只追加。
- `plugin_runtime.py:CordisRuntime.replace`：HMR 的依赖重建顺序。

运行后查看 `workspace/session-events.jsonl`：

```text
python experiments/experiment_5_agent_notes_lab/p19_deepseek_harness/main.py
```

`production.py` 的 `ProductionPluginRuntime` 增加插件版本/能力声明、依赖健康检查、重复加载保护和“先卸载依赖再卸载被依赖”的生命周期约束；`source/` 是 DeepSeek Harness 官方源码阅读快照。
