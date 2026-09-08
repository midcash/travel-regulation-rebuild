from __future__ import annotations

import copy
import json
from .domain import PACKAGE, ACTIONS


def function(name, description, properties, required=()):
    return {"type": "function", "function": {"name": name, "description": description,
        "parameters": {"type": "object", "properties": properties,
                       "required": list(required), "additionalProperties": False}}}


SCHEMAS = [
    function("query_logs", "Read request logs scoped to this ticket's tenant.", {}),
    function("get_config", "Read safe credential state for this ticket's tenant.", {}),
    function("list_deployments", "Read recent deployments and known error signatures.", {}),
    function("search_runbook", "Get remediation guidance for an observed failure family.",
             {"topic": {"type": "string", "enum": list(ACTIONS)}}, ["topic"]),
    function("finish", "Submit evidence-grounded diagnosis or handoff. Does not execute remediation.", {
        "status": {"type": "string", "enum": ["diagnosed", "handoff"]},
        "root_cause": {"type": "string", "enum": list(ACTIONS)},
        "action": {"type": "string", "enum": list(ACTIONS.values())},
        "evidence_ids": {"type": "array", "items": {"type": "string"}}},
        ["status", "root_cause", "action", "evidence_ids"]),
]


class Tools:
    def __init__(self, scenario):
        # Scope is bound here; no tenant/path/SQL is accepted from the model.
        self._scenario = copy.deepcopy(scenario)
        self._faults = dict(scenario.get("faults", {}))
        self.seen = {}
        self.logs_available = False
        self.calls = 0

    def execute(self, name, args):
        self.calls += 1
        if name not in {"query_logs", "get_config", "list_deployments", "search_runbook"}:
            raise ValueError("Unknown tool")
        expected = {"topic"} if name == "search_runbook" else set()
        if not isinstance(args, dict) or set(args) != expected:
            raise ValueError("Unexpected tool arguments")
        if name == "search_runbook" and (not isinstance(args["topic"], str) or args["topic"] not in ACTIONS):
            raise ValueError("Invalid topic")
        if self._faults.get(name, 0) > 0:
            self._faults[name] -= 1
            raise TimeoutError("Simulated backend timeout")
        if name == "query_logs":
            records = self._scenario["logs"]
            self.logs_available = True
        elif name == "get_config":
            records = [self._scenario["config"]]
        elif name == "list_deployments":
            records = self._scenario["deployments"]
        else:
            books = json.loads((PACKAGE / "data/runbooks.json").read_text(encoding="utf-8"))
            records = [x for x in books if x["topic"] == args["topic"]]
        records = copy.deepcopy(records)
        for record in records:
            record["source"] = name
            self.seen[record["id"]] = record
        return {"records": records}
