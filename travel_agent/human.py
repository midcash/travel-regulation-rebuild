"""Human input validation, separate from supplier facts and tool execution."""
import copy
import re

from .tools import function


HUMAN_SCHEMA = function("request_human_input",
    "请求候选选择、确认或偏好补充。选项只能引用已查询的 evidence_id；真实交通、酒店或地点候选另给 candidate_id。不得要求修改核心需求。",
    {"question": {"type": "string"},
     "kind": {"type": "string", "enum": ["select", "confirm", "preference"]},
     "options": {"type": "array", "items": {"type": "object", "properties": {
         "option_id": {"type": "string"}, "evidence_id": {"type": "string"},
         "candidate_id": {"type": "string"}}, "required": ["option_id", "evidence_id"],
         "additionalProperties": False}}}, ["question", "kind", "options"])


class HumanInputError(ValueError):
    pass


def validate_messages(messages, *, allow_pending=False):
    used, pending = set(), None
    for message in messages:
        if message.get("role") not in {"system", "user", "assistant", "tool"}:
            raise ValueError("Invalid message role")
        if "tool_calls" in message and message.get("role") != "assistant":
            raise ValueError("Only assistant messages may request tools")
        if message.get("role") == "assistant":
            if pending is not None:
                raise ValueError("Missing tool response")
            calls = message.get("tool_calls", [])
            if len(calls) != 1:
                raise ValueError("Exactly one call required")
            identifier = calls[0].get("id")
            if not isinstance(identifier, str) or not identifier or identifier in used:
                raise ValueError("Invalid or duplicate model call ID")
            used.add(identifier)
            pending = identifier
        elif message.get("role") == "tool":
            if pending is None or message.get("tool_call_id") != pending:
                raise ValueError("Unmatched tool response")
            pending = None
        elif pending is not None:
            raise ValueError("Context interrupts pending tool response")
    if pending is not None and not allow_pending:
        raise ValueError("Pending tool response")


def prepare_question(args, seen):
    if not isinstance(args, dict) or set(args) != {"question", "kind", "options"}:
        raise HumanInputError("Invalid human request")
    if args["kind"] not in {"select", "confirm", "preference"}:
        raise HumanInputError("Invalid interaction kind")
    if not isinstance(args["question"], str) or not 1 <= len(args["question"].strip()) <= 1000:
        raise HumanInputError("Invalid question")
    options = args["options"]
    if not isinstance(options, list) or len(options) > 20 or (args["kind"] == "select") != bool(options):
        raise HumanInputError("Selection requires observed options")
    result = {**args, "options": []}
    identifiers = set()
    for option in options:
        if not isinstance(option, dict) or set(option) - {"option_id", "evidence_id", "candidate_id"}:
            raise HumanInputError("Invalid option")
        identifier, ref = option.get("option_id"), option.get("evidence_id")
        if not isinstance(identifier, str) or not 1 <= len(identifier) <= 120 or identifier in identifiers:
            raise HumanInputError("Invalid option ID")
        if not isinstance(ref, str) or ref not in seen:
            raise HumanInputError("Unobserved option")
        record = seen[ref]
        candidate_id = option.get("candidate_id")
        if candidate_id is not None:
            candidates = record.get("data", {}).get("candidates", [])
            record = next((x for x in candidates if x.get("candidate_id") == candidate_id), None)
            if record is None:
                raise HumanInputError("Unobserved candidate")
        elif "data" in record:
            raise HumanInputError("Select a specific candidate, not an entire supplier response")
        identifiers.add(identifier)
        result["options"].append({**option, "record": copy.deepcopy(record)})
    return result


def validate_reply(question, reply):
    if not isinstance(reply, dict):
        raise HumanInputError("Reply must be an object")
    if set(reply) - {"option_id", "confirmed", "preference", "rejected"}:
        raise HumanInputError("核心需求变更需要新建任务；当前交互尚未消费。")
    text = reply.get("preference", "")
    if not isinstance(text, str) or len(text) > 1000:
        raise HumanInputError("Invalid preference")
    # Conservative rejection is deliberate: free text is not a request editor.
    if re.search(r"\d{4}[-年/]\d|改.{0,12}(日期|时间|城市|会场|地点|飞机|火车|住宿)|(日期|时间|城市|会场|地点).{0,8}(改|换)|换.{0,8}(城市|会场|日期)|change.{0,20}(date|city|meeting)|取消任务", text, re.I):
        raise HumanInputError("核心需求变更需要新建任务；当前交互尚未消费。")
    kind = question["kind"]
    allowed = {"select": {"option_id", "preference", "rejected"}, "confirm": {"confirmed", "preference"}, "preference": {"preference"}}[kind]
    if set(reply) - allowed:
        raise HumanInputError("Reply does not match question")
    if kind == "select":
        if reply.get("rejected") is True and "option_id" not in reply:
            return copy.deepcopy(reply)
        if "rejected" in reply:
            raise HumanInputError("Choose an option or reject the candidates")
        selected = next((x for x in question["options"] if x["option_id"] == reply.get("option_id")), None)
        if selected is None:
            raise HumanInputError("Unknown option")
        for other in question["options"]:
            if other is selected:
                continue
            label = other["record"].get("label", "")
            if (label and label in text) or re.search(r"(?:选择|选|select)\s*" + re.escape(other["option_id"]) + r"(?:\b|$)", text, re.I):
                raise HumanInputError("偏好与选择冲突，请澄清后重新回复。")
    elif kind == "confirm" and type(reply.get("confirmed")) is not bool:
        raise HumanInputError("Confirmation must be true or false")
    elif kind == "preference" and not text.strip():
        raise HumanInputError("Preference required")
    return copy.deepcopy(reply)


def candidate_question(execution, decisions):
    """Programmatic ambiguity gate for observed transport and stay candidates."""
    if execution.tool_mode != "real" or not execution.toolbox.history:
        return None
    result = next(reversed(execution.toolbox.history.values()))
    if result["tool"] not in {"search_transport", "search_stays", "resolve_place"} or result["status"] != "ok":
        return None
    refs = result["evidence_refs"]
    candidates = result["data"]["candidates"]
    if len(candidates) < 2 or not refs or any(d.get("candidate_call_id") == result["call_id"] for d in decisions):
        return None
    label = {"search_stays": "住宿", "search_transport": "交通", "resolve_place": "地点"}[result["tool"]]
    return {"kind": "select", "question": f"已查询到多个{label}候选，请选择或拒绝这些候选。", "options": [
        {"option_id": c["candidate_id"], "candidate_id": c["candidate_id"], "evidence_id": refs[0]} for c in candidates]}


def check_decisions(pending, decisions, seen):
    """Enforce accepted object identities at dispatch, including final verdicts.

    New tools must extend this explicit mapping when their schemas are added.
    """
    name, args = pending["name"], pending["args"]
    if not isinstance(args, dict):
        raise ValueError("Invalid tool arguments")
    used_ids = set(args.get("segment_ids", [])) if name == "finish" else set()
    if name == "finish":
        for entry in seen.values():
            if entry.get("source") != "calculate_route":
                continue
            data = entry.get("data")
            if not isinstance(data, dict):
                continue
            if any(isinstance(path, dict) and path.get("route_id") in used_ids
                   for path in data.get("paths", [])):
                used_ids.update(identifier for identifier in (
                    data.get("origin_candidate_id"), data.get("destination_candidate_id"))
                    if isinstance(identifier, str))
    confirmations = [d["reply"]["confirmed"] for d in decisions if "confirmed" in d.get("reply", {})]
    if name == "finish" and args.get("status") == "planned" and confirmations and confirmations[-1] is False:
        raise ValueError("Plan has not been confirmed after rejection")
    for decision in decisions:
        rejected = {x.get("candidate_id", x["evidence_id"]) for x in decision.get("rejected_options", [])}
        if (name == "finish" and args.get("status") == "planned" and rejected.intersection(used_ids)) or args.get("candidate_id") in rejected:
            raise ValueError("Tool or plan uses a rejected candidate")
        selected = decision.get("selected")
        if not selected:
            continue
        identifier = selected.get("candidate_id", selected["evidence_id"])
        if name == "finish" and args.get("status") == "planned":
            if identifier not in used_ids:
                raise ValueError("Final plan contradicts confirmed selection")
        source = seen.get(selected["evidence_id"], {}).get("source")
        detail_source = {"get_transport_detail": "search_transport", "get_stay_detail": "search_stays"}.get(name)
        if name == "resolve_place":
            detail_source = {"arrival": "search_transport", "hotel": "search_stays", "meeting": "request"}.get(args.get("role"))
        if "candidate_id" in args and (detail_source is None or source == detail_source) and args["candidate_id"] != identifier:
            raise ValueError("Tool contradicts confirmed candidate")
    if name == "calculate_route":
        # A selected place candidate is application context. If its role is
        # relevant to this route, both endpoints must use that accepted ID.
        observed_roles = {}
        for entry in seen.values():
            data = entry.get("data")
            candidates = data.get("candidates", []) if isinstance(data, dict) else []
            for candidate in candidates:
                if isinstance(candidate, dict) and isinstance(candidate.get("candidate_id"), str):
                    observed_roles[candidate["candidate_id"]] = candidate.get("role")
        accepted_by_role = {}
        for decision in decisions:
            selected = decision.get("selected") or {}
            record = selected.get("record") or {}
            role = record.get("role")
            identifier = selected.get("candidate_id")
            if role and isinstance(identifier, str):
                accepted_by_role.setdefault(role, set()).add(identifier)
        for field in ("origin_candidate_id", "destination_candidate_id"):
            endpoint = args.get(field)
            role = observed_roles.get(endpoint)
            accepted = accepted_by_role.get(role, set())
            if accepted and endpoint not in accepted:
                raise ValueError("Route contradicts confirmed place candidate")
