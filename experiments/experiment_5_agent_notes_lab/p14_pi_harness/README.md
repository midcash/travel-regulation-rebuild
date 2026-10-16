# p14 Pi-Agent Harness

对应第 14 章。Pi 采用“小内核 + 扩展”，原型只保留最适合学习的几块：AgentLoop 所需的会话存储、工具结果、回退和 Compaction。

## 你应该看见什么

- 一个会话是一个 JSONL 文件，条目只追加不改写。
- 每个条目有 `parentId`，扁平文件因此可以还原为会话树。
- 回退不是删除历史，而是把当前位置指到另一个节点；模型上下文只取当前节点到根的一条链。
- 压缩也只是追加一个 `compaction` 节点，用摘要替代较早原文。
- Pi 内核的回退只覆盖对话，不自动还原工作区代码；代码快照需要扩展或 Git。

运行后可查看 `workspace/demo-session.jsonl` 和 `workspace/demo-compaction.jsonl`。

## 建议断点

- `session_tree.py:AppendOnlySession.append`：所有持久化都经过这里。
- `session_tree.py:current_path`：树如何变成当前上下文链。
- `session_tree.py:model_context`：压缩摘要如何替代旧消息。

运行：

```text
python experiments/experiment_5_agent_notes_lab/p14_pi_harness/main.py
```

`production.py` 的 `ProductionSessionTree` 增加 parent 存在性校验、角色/内容校验、重复 event 去重和 lineage 保真压缩；`source/` 是 pi-mono 官方源码阅读快照。
