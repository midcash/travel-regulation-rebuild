"""Main-chain tools independent of experiments and synthetic datasets."""
from pathlib import Path
from .domain import PACKAGE
from .regulation_policy import RegulationPolicyError, load_regulation_policy
from .tools import SCHEMAS, function

REAL_SCHEMAS = [function("get_policy_snapshot", "读取浙江省差旅规定原文快照及来源哈希；不作合规认证。", {}), SCHEMAS[-1]]

class RealTools:
    def __init__(self, trace_id, policy_path=None):
        self.policy_path = Path(policy_path) if policy_path is not None else PACKAGE / "policies/浙江省机关工作人员差旅费管理规定.md"
        self.trace_id = trace_id
        self.calls = 0
        self.seen = {}
        self.history = {}
        self._data = {"tool_mode": "real"}

    def execute(self, name, args):
        self.calls += 1
        if name != "get_policy_snapshot" or not isinstance(args, dict) or args:
            raise ValueError("Unknown tool or unexpected arguments")
        call_id = f"{self.trace_id}:tool:{self.calls}"
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
