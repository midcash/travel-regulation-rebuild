"""Production-shaped scenario orchestration with explicit evidence and audit."""
from __future__ import annotations

import time
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from common.errors import AgentError, ErrorKind, as_agent_error
from common.storage import IdempotencyStore, JsonlStore


@dataclass(frozen=True)
class Check:
    name: str
    run: Callable[[str], tuple[bool, str]]
    critical: bool = True


@dataclass
class ScenarioDecision:
    request_id: str
    status: str
    evidence: dict[str, str]
    errors: dict[str, dict] = field(default_factory=dict)


class ProductionScenarioRunner:
    def __init__(self, checks: list[Check], root: str | Path = "workspace/scenario"):
        self.checks = checks
        root = Path(root)
        self.audit = JsonlStore(root / "audit.jsonl")
        self.idempotency = IdempotencyStore(root / "idempotency.jsonl")

    def run(self, request_id: str, input_text: str) -> ScenarioDecision:
        request_hash = hashlib.sha256(json.dumps({"input": input_text}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        status, cached = self.idempotency.begin(request_id, request_hash=request_hash)
        if status == "done":
            return ScenarioDecision(request_id, cached["status"], cached["evidence"], cached.get("errors", {}))
        if status == "in_progress":
            raise AgentError("request_in_progress", "同一个场景请求正在执行", ErrorKind.CONFLICT, True)
        evidence: dict[str, str] = {}
        errors: dict[str, dict] = {}
        with ThreadPoolExecutor(max_workers=min(8, max(1, len(self.checks)))) as pool:
            futures = {pool.submit(check.run, input_text): check for check in self.checks}
            for future in as_completed(futures):
                check = futures[future]
                try:
                    ok, text = future.result()
                    if ok:
                        evidence[check.name] = text
                    else:
                        errors[check.name] = {"code": "check_failed", "message": text}
                except BaseException as exc:
                    errors[check.name] = as_agent_error(exc).safe_dict()
        critical_failure = any((not evidence.get(check.name)) and check.critical for check in self.checks)
        decision = ScenarioDecision(request_id, "rejected" if critical_failure else "accepted", dict(sorted(evidence.items())), dict(sorted(errors.items())))
        self.idempotency.complete(request_id, vars(decision), request_hash=request_hash)
        self.audit.append({"event": "scenario.decided", "ts": time.time(), **vars(decision)})
        return decision
