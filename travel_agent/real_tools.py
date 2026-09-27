"""Main-chain tools independent of experiments and synthetic datasets."""
import asyncio
import copy
from pathlib import Path
from .transport import search_transport
from .transport_detail import get_transport_detail
from .stays import search_stays, get_stay_detail
from .places import resolve_place
from .route import calculate_route
from .domain import PACKAGE, fingerprint
from .regulation_policy import RegulationPolicyError, load_regulation_policy
from .tools import SCHEMAS, function

REAL_FINISH = function("finish", "提交经证据与时间校验的完整真实行程，或说明本次查询资料不足/无可行候选。", {
    "status": {"type": "string", "enum": ["planned", "unavailable", "needs_review"]},
    "reason": {"type": "string", "enum": ["feasible", "no_feasible_route", "missing_data"]},
    "segment_ids": {"type": "array", "items": {"type": "string"}},
    "evidence_ids": {"type": "array", "items": {"type": "string"}},
    "planning": {"type": "object", "properties": {
        "hotel_departure_at": {"type": "string", "description": "ISO 8601 planned hotel departure with timezone"},
        "arrival_buffer_minutes": {"type": "integer", "minimum": 0, "maximum": 1440}},
        "required": ["hotel_departure_at", "arrival_buffer_minutes"], "additionalProperties": False}},
    ["status", "reason", "segment_ids", "evidence_ids"])

REAL_SCHEMAS = [function("get_policy_snapshot", "读取浙江省差旅规定原文快照及来源哈希；不作合规认证。", {}), function("search_transport", "搜索真实航班或火车；返回供应商时刻、请求约束检查及证据，不代表完整方案。", {
    "mode": {"type": "string", "enum": ["flight", "train"]},
    "origin": {"type": "string"}, "destination": {"type": "string"},
    "departure_date": {"type": "string", "description": "YYYY-MM-DD, Asia/Shanghai"},
    "service_class": {"type": "string", "enum": ["any", "high_speed_rail"]},
    "max_results": {"type": "integer", "minimum": 1, "maximum": 20}},
    ["mode", "origin", "destination", "departure_date"]), function("get_transport_detail", "查询本次已观察交通候选的机场、航站楼及席别报价；只传 candidate_id，不改写原候选条件。", {
    "candidate_id": {"type": "string"}, "max_options": {"type": "integer", "minimum": 1, "maximum": 20}},
    ["candidate_id"]), function("search_stays", "查询住宿候选；日期是查询条件，默认1成人1间房，未知价格口径不作总价。", {
    "destination": {"type": "string"}, "check_in": {"type": "string"}, "check_out": {"type": "string"},
    "meeting_place": {"type": "string"}, "max_results": {"type": "integer", "minimum": 1, "maximum": 20}},
    ["destination", "check_in", "check_out"]), function("get_stay_detail", "查询本次已观察酒店候选的房型报价，沿用原住宿日期及人数。", {
    "candidate_id": {"type": "string"}}, ["candidate_id"]), function("resolve_place", "解析本次到达地点、酒店或会场；role必填，arrival/hotel须给已观察candidate_id，会场来自原请求；city/keyword省略时从证据推导，提供时须一致。未经人工确认。", {
    "role": {"type": "string", "enum": ["arrival", "hotel", "meeting"]},
    "candidate_id": {"type": "string"}, "city": {"type": "string"}, "keyword": {"type": "string"},
    "max_results": {"type": "integer", "minimum": 1, "maximum": 20}}, ["role"]), function("calculate_route", "使用本次运行已解析地点候选的 GCJ-02 坐标查询驾车路线；只连接到达地点→酒店或酒店→会议地点。耗时是查询时估计，未知费用保持为空。", {
    "origin_candidate_id": {"type": "string"}, "destination_candidate_id": {"type": "string"},
    "origin": {"type": "string", "description": "兼容旧调用；必须与已观察地点坐标完全一致"},
    "destination": {"type": "string", "description": "兼容旧调用；必须与已观察地点坐标完全一致"},
    "origin_id": {"type": "string"}, "destination_id": {"type": "string"},
    "max_paths": {"type": "integer", "minimum": 1, "maximum": 10}},
    []), REAL_FINISH]

class RealTools:
    STATE_FIELDS = ("calls", "http_requests", "seen", "history", "_transport_attempts", "_data")

    def snapshot(self):
        return copy.deepcopy({key: getattr(self, key) for key in self.STATE_FIELDS})

    def restore(self, state):
        if set(state) != set(self.STATE_FIELDS):
            raise ValueError("Incompatible real tool state")
        for key in self.STATE_FIELDS:
            setattr(self, key, copy.deepcopy(state[key]))

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
        self._transport_attempts = {}
        self._data = {"tool_mode": "real"}

    def execute(self, name, args, timeout=45):
        self.calls += 1
        call_id = f"{self.trace_id}:tool:{self.calls}"
        if name in {"search_transport", "get_transport_detail", "search_stays", "get_stay_detail", "resolve_place", "calculate_route"}:
            operation = {"search_transport": search_transport, "get_transport_detail": get_transport_detail,
                         "search_stays": search_stays, "get_stay_detail": get_stay_detail,
                         "resolve_place": resolve_place, "calculate_route": calculate_route}[name]
            context = self.request if name in {"search_transport", "search_stays"} else self.history
            if name in {"resolve_place", "calculate_route"}:
                context = (self.request, self.history)
            result, record = asyncio.run(operation(args, context, call_id,
                self.evidence_dir, timeout, self.transport_http))
            self.http_requests += result["http_requests"]
            # Omitted defaults and explicit defaults describe the same query.
            defaults = {"search_transport": {"service_class": "any", "max_results": 5},
                        "get_transport_detail": {"max_options": 20}, "search_stays": {"max_results": 5},
                        "get_stay_detail": {}, "resolve_place": {"max_results": 5},
                        "calculate_route": {"max_paths": 3}}[name]
            query_key = fingerprint({"tool": name, **defaults, **args})
            previous = self._transport_attempts.get(query_key)
            is_retry = (previous is not None and previous["status"] == "error"
                        and previous["http_requests"] > 0 and result["http_requests"] > 0)
            result["attempt"] = previous["attempt"] + 1 if is_retry else 1
            result["retry_of"] = previous["call_id"] if is_retry else None
            self._transport_attempts[query_key] = result
            evidence_records = {item.get("call_id"): item for item in record.get("requests", [])
                                if isinstance(item, dict)} if isinstance(record, dict) else {}
            for identifier in result["evidence_refs"]:
                source_record = evidence_records.get(identifier, record)
                self.seen[identifier] = {"id": identifier, "source": name, "call_id": call_id,
                    "data": result["data"], "supplier_evidence": {k: v for k, v in source_record.items() if k != "wire_text"}}
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
