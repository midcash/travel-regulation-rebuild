"""最小业务校验层。

实验四要验证的是暂停恢复机制，不是业务规则。所以这一层只保留一条业务约束：
**引用的证据必须真的见过**。其余全部交给 domain.validate_verdict 那种严格校验的场合。

这样做是有意的：如果测试挂了，问题必然出在机制上，不会和业务规则纠缠。
接入主链路时，把 validate_finish 换成 domain.validate_verdict 即可，
它的输入（seen / tool_results）都已经在快照里了。
"""
from __future__ import annotations

from .tools import VALID_REASON, VALID_STATUS

# 与 domain.handoff() 同形：异常终止时的兜底结论。
HANDOFF = {"status": "needs_review", "reason": "execution_failed",
           "evidence_ids": [], "claims": []}


def handoff():
    return {"status": HANDOFF["status"], "reason": HANDOFF["reason"],
            "evidence_ids": [], "claims": []}


def validate_finish(args, seen):
    if not isinstance(args, dict) or set(args) != {"status", "reason", "evidence_ids"}:
        raise ValueError("Invalid verdict schema")
    if args["status"] not in VALID_STATUS:
        raise ValueError("Invalid status")
    if args["reason"] not in VALID_REASON:
        raise ValueError("Invalid reason")
    if (args["status"] == "planned") != (args["reason"] == "feasible"):
        raise ValueError("Inconsistent status and reason")
    ids = args["evidence_ids"]
    if (not isinstance(ids, list) or len(ids) > 20
            or any(not isinstance(x, str) for x in ids)
            or len(set(ids)) != len(ids) or not set(ids).issubset(seen)):
        raise ValueError("Unobserved or duplicate evidence")
    # 不许零证据宣称成功。少了这条，模型可以直接 finish(planned, feasible, [])，
    # 而 validate 放行——真实模型测试里的"成功"也就没有意义了。
    if args["status"] == "planned" and not ids:
        raise ValueError("A planned verdict must cite evidence")
    return {"status": args["status"], "reason": args["reason"],
            "evidence_ids": list(ids), "claims": [seen[x] for x in ids]}
