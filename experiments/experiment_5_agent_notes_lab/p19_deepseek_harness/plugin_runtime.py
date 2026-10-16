"""DeepSeekHarness 原型：Cordis 风格插件生命周期 + append-only SessionEventLog。

这里不实现真实 Cordis，而是保留最有学习价值的结构：
插件由 Runtime 装配，Fiber 管生命周期，Context 提供服务槽，Effect 负责清理，
AgentLoop 和会话事件也只是可替换模块。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


class SessionEventLog:
    """会话唯一事实来源：事件只追加，消息历史从事件派生。"""

    def __init__(self, path: str | Path, reset: bool = False) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if reset:
            self.path.write_text("", encoding="utf-8")
        self.events: list[dict[str, Any]] = []
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self.events.append(json.loads(line))

    def append(self, event_type: str, **data: Any) -> dict[str, Any]:
        event = {"seq": len(self.events) + 1, "type": event_type, **data}
        self.events.append(event)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        return event

    def derive_messages(self) -> list[dict[str, str]]:
        """只从事实事件派生给模型或 UI 的消息视图。"""
        result: list[dict[str, str]] = []
        roles = {"user_message": "user", "assistant_message": "assistant", "tool_result": "tool"}
        for event in self.events:
            if event["type"] in {"user_message", "assistant_message", "tool_result"}:
                result.append({"role": roles[event["type"]], "content": str(event["content"])})
        return result


@dataclass
class Fiber:
    plugin: "Plugin"
    state: str = "Pending"
    provided: set[str] = field(default_factory=set)
    effects: list[Callable[[], None]] = field(default_factory=list)


class Context:
    def __init__(self, runtime: "CordisRuntime", fiber: Fiber) -> None:
        self.runtime = runtime
        self.fiber = fiber

    def provide(self, name: str, value: Any) -> None:
        self.runtime.services[name] = value
        self.runtime.owners[name] = self.fiber.plugin.name
        self.fiber.provided.add(name)

    def require(self, name: str) -> Any:
        return self.runtime.services[name]

    def effect(self, cleanup: Callable[[], None]) -> None:
        self.fiber.effects.append(cleanup)


class Plugin:
    def __init__(self, name: str, requires: tuple[str, ...] = ()) -> None:
        self.name = name
        self.requires = requires

    def apply(self, ctx: Context) -> None:
        raise NotImplementedError


class ModelPlugin(Plugin):
    def __init__(self, name: str, model_name: str, suffix: str) -> None:
        super().__init__(name)
        self.model_name = model_name
        self.suffix = suffix

    def apply(self, ctx: Context) -> None:
        def model(prompt: str) -> str:
            if "工具结果" in prompt:
                return f"{self.model_name} 最终回答：{prompt.split('工具结果', 1)[1].strip()}（{self.suffix}）"
            return "需要调用 lookup 工具"

        ctx.provide("llm", model)
        ctx.effect(lambda: None)  # 真实插件这里会关闭 provider 连接。


class ToolPlugin(Plugin):
    def __init__(self) -> None:
        super().__init__("tools")

    def apply(self, ctx: Context) -> None:
        ctx.provide("tools", {"lookup": lambda query: f"知识库结果：{query} = 42"})
        ctx.effect(lambda: None)


class AgentLoopPlugin(Plugin):
    def __init__(self, event_log: SessionEventLog) -> None:
        super().__init__("agent-loop", requires=("llm", "tools"))
        self.event_log = event_log

    def apply(self, ctx: Context) -> None:
        llm = ctx.require("llm")
        tools = ctx.require("tools")

        def run(prompt: str) -> str:
            self.event_log.append("user_message", content=prompt)
            self.event_log.append("model_input", content=prompt)
            plan = llm(prompt)
            self.event_log.append("model_output", content=plan)
            self.event_log.append("tool_call", name="lookup", arguments={"query": prompt})
            result = tools["lookup"](prompt)
            self.event_log.append("tool_result", content=result, name="lookup")
            final_prompt = f"工具结果 {result}"
            self.event_log.append("model_input", content=final_prompt)
            answer = llm(final_prompt)
            self.event_log.append("model_output", content=answer)
            self.event_log.append("assistant_message", content=answer)
            return answer

        ctx.provide("agent_loop", run)
        ctx.effect(lambda: None)


class CordisRuntime:
    """插件加载、依赖检查、Fiber 状态和 Effect 清理。"""

    def __init__(self) -> None:
        self.services: dict[str, Any] = {}
        self.owners: dict[str, str] = {}
        self.fibers: dict[str, Fiber] = {}
        self.definitions: dict[str, Plugin] = {}
        self.lifecycle: list[str] = []

    def load(self, plugin: Plugin) -> None:
        if plugin.name in self.fibers:
            raise ValueError(f"插件已加载: {plugin.name}")
        missing = [name for name in plugin.requires if name not in self.services]
        fiber = Fiber(plugin)
        self.fibers[plugin.name] = fiber
        self.definitions[plugin.name] = plugin
        if missing:
            fiber.state = "Pending"
            self.lifecycle.append(f"{plugin.name}:pending({','.join(missing)})")
            raise RuntimeError(f"{plugin.name} 缺少依赖: {missing}")
        fiber.state = "Loading"
        self.lifecycle.append(f"{plugin.name}:loading")
        plugin.apply(Context(self, fiber))
        fiber.state = "Active"
        self.lifecycle.append(f"{plugin.name}:active")

    def unload(self, name: str) -> None:
        fiber = self.fibers[name]
        fiber.state = "Unloading"
        self.lifecycle.append(f"{name}:unloading")
        for cleanup in reversed(fiber.effects):
            cleanup()
        for service in fiber.provided:
            if self.owners.get(service) == name:
                self.services.pop(service, None)
                self.owners.pop(service, None)
        fiber.state = "Disposed"
        self.lifecycle.append(f"{name}:disposed")
        del self.fibers[name]

    def replace(self, name: str, replacement: Plugin) -> None:
        provided_by_old = {service for service, owner in self.owners.items() if owner == name}
        dependents = [
            plugin
            for plugin in self.definitions.values()
            if plugin.name in self.fibers and set(plugin.requires) & provided_by_old
        ]
        for plugin in reversed(dependents):
            self.unload(plugin.name)
        self.unload(name)
        self.load(replacement)
        for plugin in dependents:
            self.load(plugin)

    def active_services(self) -> dict[str, str]:
        return {name: self.owners[name] for name in self.services}
