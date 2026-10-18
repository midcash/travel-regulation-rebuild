"""原型 19：DeepSeekHarness 的 Everything is a Plugin 与事件日志。"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from common import trace  # noqa: E402
from plugin_runtime import AgentLoopPlugin, CordisRuntime, ModelPlugin, SessionEventLog, ToolPlugin  # noqa: E402


ROOT = pathlib.Path(__file__).resolve().parent


def main() -> None:
    trace.section("原型 19：DeepSeekHarness 插件运行时", "第 19 章：Cordis / Everything is a Plugin / append-only EventLog")
    event_log = SessionEventLog(ROOT / "workspace" / "session-events.jsonl", reset=True)
    runtime = CordisRuntime()
    runtime.load(ModelPlugin("model", "DeepSeek", "v1"))
    runtime.load(ToolPlugin())
    loop_plugin = AgentLoopPlugin(event_log)
    runtime.load(loop_plugin)

    trace.section("静态组装：Profile/配置 → Plugin → Fiber → Context 服务")
    trace.kv("Fiber 状态", {name: fiber.state for name, fiber in runtime.fibers.items()})
    trace.kv("服务槽", runtime.active_services())
    answer = runtime.services["agent_loop"]("查询旅行政策")
    trace.kv("AgentLoop 最终结果", answer)

    trace.section("append-only SessionEventLog")
    trace.kv("事件类型", [event["type"] for event in event_log.events])
    trace.kv("从事件派生的消息", event_log.derive_messages())
    trace.note("模型输入、模型输出、工具调用和工具结果都追加为事实；消息历史是派生视图，不是第二份真相。")

    trace.section("HMR：替换模型插件并重建依赖它的 AgentLoop")
    runtime.replace("model", ModelPlugin("model", "DeepSeek", "v2-hot-reloaded"))
    trace.kv("重载后的 Fiber", {name: fiber.state for name, fiber in runtime.fibers.items()})
    trace.kv("生命周期", runtime.lifecycle)
    answer2 = runtime.services["agent_loop"]("查询退款规则")
    trace.kv("新模型结果", answer2)
    trace.note("HMR 不是简单覆盖对象：先卸载依赖、执行 Effect 清理，再加载替代插件并重新激活依赖。")


if __name__ == "__main__":
    main()
