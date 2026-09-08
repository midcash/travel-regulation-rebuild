from __future__ import annotations

import hashlib
import json
from pathlib import Path

PACKAGE = Path(__file__).parent
ACTIONS = {"rate_limit": "backoff", "credential_revoked": "rotate_credentials",
           "deployment_regression": "review_rollback", "unknown": "escalate"}
DRAFTS = {
    "rate_limit": "发现接口限流证据。建议遵守 Retry-After 并使用有上限的退避重试；重放前由人工确认幂等性。",
    "credential_revoked": "发现凭据已撤销及鉴权失败证据。请授权管理员轮换凭据，无需在工单中提供密钥。",
    "deployment_regression": "发现服务端错误与发布记录中的已知错误签名匹配。建议人工审核回滚方案，本次未执行回滚。",
    "unknown": "当前证据不足、冲突或调查未能完成，已转人工。请补充请求编号和故障时间范围。",
}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def scenarios():

    return json.loads((PACKAGE / "data/scenarios.json").read_text(encoding="utf-8"))


def scenario_by_id(identifier):
    for item in scenarios():
        if item["id"] == identifier:
            return item
    raise ValueError("Unknown scenario")


def handoff():
    return {"status": "handoff", "root_cause": "unknown", "action": "escalate", "evidence_ids": []}


def validate_verdict(value, seen, logs_available):
    if not isinstance(value, dict) or set(value) != {"status", "root_cause", "action", "evidence_ids"}:
        raise ValueError("Invalid verdict schema")
    root = value["root_cause"]
    if not isinstance(root, str) or root not in ACTIONS or value["action"] != ACTIONS[root]:
        raise ValueError("Unsupported root cause or unsafe action")
    expected_status = "handoff" if root == "unknown" else "diagnosed"
    if value["status"] != expected_status:
        raise ValueError("Inconsistent status")
    refs = value["evidence_ids"]
    if not isinstance(refs, list) or len(refs) > 30 or any(not isinstance(x, str) for x in refs):
        raise ValueError("Invalid evidence list")
    if len(set(refs)) != len(refs) or not set(refs).issubset(seen):
        raise ValueError("Unobserved or duplicate evidence")
    if root != "unknown":
        if not logs_available or not refs:
            raise ValueError("Diagnosis requires observed logs")
        records = [seen[x] for x in refs]
        if not any(x.get("source") == "query_logs" for x in records):
            raise ValueError("Diagnosis requires a log citation")
        if not any(x.get("source") == "search_runbook" and x.get("topic") == root for x in records):
            raise ValueError("Diagnosis requires relevant runbook")
    return dict(value)
