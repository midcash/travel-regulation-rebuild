"""Cordis-inspired plugin runtime with lifecycle, event log and dependency checks."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from common.errors import AgentError, ErrorKind
from common.storage import JsonlStore


@dataclass(frozen=True)
class PluginSpec:
    name: str
    version: str
    dependencies: tuple[str, ...] = ()
    capabilities: frozenset[str] = frozenset()


@dataclass
class PluginStatus:
    spec: PluginSpec
    loaded: bool = False
    healthy: bool = False
    error: str = ""


class ProductionPluginRuntime:
    def __init__(self, path: str | Path = "workspace/plugin-events.jsonl"):
        self.events = JsonlStore(path)
        self.plugins: dict[str, PluginStatus] = {}
        self._replay()

    def _replay(self) -> None:
        for event in self.events.read():
            if event.get("type") == "plugin.loaded" and event.get("plugin"):
                spec = PluginSpec(
                    str(event["plugin"]),
                    str(event.get("version", "unknown")),
                    tuple(str(item) for item in event.get("dependencies", [])),
                    frozenset(str(item) for item in event.get("capabilities", [])),
                )
                self.plugins[spec.name] = PluginStatus(spec, loaded=True, healthy=True)
            elif event.get("type") == "plugin.unloaded" and event.get("plugin"):
                self.plugins.pop(str(event["plugin"]), None)

    def load(self, spec: PluginSpec) -> PluginStatus:
        if spec.name in self.plugins:
            raise AgentError("duplicate_plugin", f"插件 {spec.name!r} 已加载", ErrorKind.CONFLICT)
        missing = [item for item in spec.dependencies if item not in self.plugins or not self.plugins[item].healthy]
        if missing:
            raise AgentError("missing_dependency", "插件依赖未满足", ErrorKind.CONFLICT, details={"missing": missing})
        status = PluginStatus(spec, loaded=True, healthy=True)
        self.plugins[spec.name] = status
        self.events.append({"type": "plugin.loaded", "plugin": spec.name, "version": spec.version, "dependencies": list(spec.dependencies), "capabilities": sorted(spec.capabilities), "ts": time.time()})
        return status

    def unload(self, name: str) -> None:
        status = self.plugins.get(name)
        if status is None:
            return
        dependents = [item.spec.name for item in self.plugins.values() if name in item.spec.dependencies]
        if dependents:
            raise AgentError("plugin_in_use", "卸载前必须先处理依赖插件", ErrorKind.CONFLICT, details={"dependents": dependents})
        self.plugins.pop(name)
        self.events.append({"type": "plugin.unloaded", "plugin": name, "ts": time.time()})
