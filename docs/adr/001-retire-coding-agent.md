# ADR 001：退役 pi-coding-agent

状态：已决定，2026-11-04，落实用户明确决定。

PR #54 建立独立 Pi Durable MVP。Pi Durable 是唯一维护的 Agent 运行路径，pi-coding-agent 集成正式退役。不补写此前未记录的选型原因。

删除旧 Worker、Python JSONL 桥和内存运行时、FastAPI 页面、根 npm 依赖及锁、旧探针与专属测试，更新检查、CI、安装与配置。不建立兼容框架，也不归档整套源码。

保留 Durable MVP、有效探针、pi-ai/chord 依赖、RealTools、供应商适配、事实结构、Trace、独立工具测试、业务语义及 B01–B12。旧回归的业务意图转为 verification/baseline/README.md 的待接入场景。

差旅业务接入属于后续独立工作；本次不要求功能对等，不代表业务迁移完成。历史数据库、Trace、密钥和个人数据不属于源码清理范围。

清理前最后包含完整旧实现的实际 main 提交：`d4b4b3b`（PR #54 合并）。例如 `git show d4b4b3b:travel_agent/runtime.py` 可查阅旧源码。
