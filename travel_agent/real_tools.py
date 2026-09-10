"""Main-chain tools independent of experiments and synthetic datasets."""
import asyncio
from pathlib import Path
from .transport import search_transport
from .domain import PACKAGE
from .regulation_policy import RegulationPolicyError, load_regulation_policy
from .tools import SCHEMAS, function

REAL_SCHEMAS = [function("get_policy_snapshot", "读取浙江省差旅规定原文快照及来源哈希；不作合规认证。", {}), function("search_transport", "搜索真实航班或火车；返回供应商时刻、请求约束检查及证据，不代表完整方案。", {
    "mode": {"type": "string", "enum": ["flight", "train"]},
    "origin": {"type": "string"}, "destination": {"type": "string"},
    "departure_date": {"type": "string", "description": "YYYY-MM-DD, Asia/Shanghai"},
    "service_class": {"type": "string", "enum": ["any", "high_speed_rail"]},
    "max_results": {"type": "integer", "minimum": 1, "maximum": 20}},
    ["mode", "origin", "destination", "departure_date"]), SCHEMAS[-1]]

class RealTools:
    def __init__(self, trace_id, policy_path=None, *, request=None, evidence_dir=None, transport_http=None):
        self.policy_path = Path(policy_path) if policy_path is not None else PACKAGE / "policies/浙江省机关工作人员差旅费管理规定.md"
        self.trace_id = trace_id
        self.calls = 0
        self.http_requests = 0
        self.request = request
        self.evidence_dir = Path(evidence_dir) if evidence_dir is not None else Path("artifacts") / trace_id
        self.transport_http = transport_http
        self.seen = {}
        self.history = {}
        self._data = {"tool_mode": "real"}

    def execute(self, name, args, timeout=45):
        self.calls += 1
        call_id = f"{self.trace_id}:tool:{self.calls}"
        if name == "search_transport":
            result, record = asyncio.run(search_transport(args, self.request, call_id,
                self.evidence_dir, timeout, self.transport_http))
            self.http_requests += result["http_requests"]
            for identifier in result["evidence_refs"]:
                self.seen[identifier] = {"id": identifier, "source": name, "call_id": call_id,
                    "data": result["data"], "supplier_evidence": {k: v for k, v in record.items() if k != "wire_text"}}
            self.history[call_id] = result
            return result
        if name != "get_policy_snapshot" or not isinstance(args, dict) or args:
            raise ValueError("Unknown tool or unexpected arguments")
        result = {"call_id": call_id, "tool": name, "status": "ok", "data": None,
                  "evidence_refs": [], "warnings": []}
        try:
            result["data"] = load_regulation_policy(self.policy_path)
        except (RegulationPolicyError, OSError, ValueError):
            result.update(status="error", error={"type": "PolicyReadError",
                "message": "政策文件缺失、不可读或格式无效；未读取替代政策。", "retryable": False})
        else:
            identifier = f"{call_id}:policy"
            result["evidence_refs"] = [identifier]
            self.seen[identifier] = {"id": identifier, "source": name, "call_id": call_id,
                                    "data": result["data"]}
        self.history[call_id] = result
        return result
