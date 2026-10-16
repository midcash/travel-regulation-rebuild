"""Durable finite-state loop graph with explicit transitions and checkpoints."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from common.errors import AgentError, ErrorKind
from common.storage import JsonlStore


@dataclass(frozen=True)
class Node:
    name: str
    handler: Callable[[dict[str, Any]], str]
    terminal: bool = False


@dataclass
class GraphRun:
    run_id: str
    state: str
    steps: int = 0
    history: list[str] = field(default_factory=list)


class ProductionGraph:
    def __init__(self, nodes: list[Node], transitions: dict[str, set[str]], *, checkpoint_path: str | Path = "workspace/graph-events.jsonl", max_steps: int = 20):
        self.nodes = {node.name: node for node in nodes}
        self.transitions = {key: set(value) for key, value in transitions.items()}
        self.events = JsonlStore(checkpoint_path)
        self.max_steps = max_steps
        if not self.nodes or max_steps < 1:
            raise ValueError("graph requires nodes and positive max_steps")

    def start(self, run_id: str, initial: str, context: dict[str, Any]) -> GraphRun:
        if initial not in self.nodes:
            raise AgentError("unknown_node", f"未知节点 {initial}", ErrorKind.VALIDATION)
        if any(event.get("type") == "run_started" and event.get("run_id") == run_id for event in self.events.read()):
            return self.restore(run_id)
        run = GraphRun(run_id, initial)
        self.events.append({"type": "run_started", "run_id": run_id, "state": initial, "ts": time.time()})
        return self.step(run, context)

    def restore(self, run_id: str) -> GraphRun:
        """Rebuild the last checkpoint after a worker restart."""
        run: GraphRun | None = None
        for event in self.events.read():
            if event.get("run_id") != run_id:
                continue
            if event.get("type") == "run_started":
                run = GraphRun(run_id, str(event["state"]))
            elif event.get("type") == "transition" and run is not None:
                run.history.append(str(event["from"]))
                run.state = str(event["to"])
                run.steps = int(event.get("step", run.steps + 1))
        if run is None:
            raise AgentError("run_not_found", "找不到可恢复的图运行", ErrorKind.NOT_FOUND)
        return run

    def step(self, run: GraphRun, context: dict[str, Any]) -> GraphRun:
        if run.state not in self.nodes:
            raise AgentError("unknown_node", "运行状态不在图定义中", ErrorKind.CONFLICT)
        if self.nodes[run.state].terminal:
            return run
        if run.steps >= self.max_steps:
            raise AgentError("step_limit", "图执行超过最大步数", ErrorKind.TIMEOUT)
        current = run.state
        next_state = self.nodes[current].handler(context)
        if next_state not in self.nodes or next_state not in self.transitions.get(current, set()):
            raise AgentError("invalid_transition", f"非法迁移: {current} -> {next_state}", ErrorKind.CONFLICT)
        run.history.append(current)
        run.state = next_state
        run.steps += 1
        self.events.append({"type": "transition", "run_id": run.run_id, "from": current, "to": next_state, "step": run.steps, "ts": time.time()})
        return run
