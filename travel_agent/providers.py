from __future__ import annotations

import copy
import json
import math
import os
import urllib.request
import urllib.error
from urllib.parse import urlsplit
from .domain import ACTIONS, handoff


def tool_call(name, args=None):
    return {"role": "assistant", "content": "", "tool_calls": [
        {"function": {"name": name, "arguments": args or {}}}]}


class Baseline:
    """Deterministic policy for plumbing and comparison; explicitly NOT an LLM agent."""
    name = "baseline"
    model = "rules-v1"

    def chat(self, messages, schemas, timeout):
        observations = {}
        for message in messages:
            if message["role"] == "tool":
                observations[message["tool_name"]] = json.loads(message["content"])
        if "query_logs" not in observations:
            reply = tool_call("query_logs")
        else:
            logs = observations["query_logs"].get("records", [])
            failed = [x for x in logs if x.get("status", 0) >= 400]
            families = {"rate" if x["status"] == 429 else "auth" if x["status"] == 401
                        else "server" if x["status"] >= 500 else "other" for x in failed}
            root, refs = "unknown", []
            if families == {"rate"}:
                root, refs = "rate_limit", [x["id"] for x in failed]
            elif families == {"auth"}:
                if "get_config" not in observations:
                    return {"message": tool_call("get_config"), "input_tokens": 0, "output_tokens": 0}
                configs = observations["get_config"].get("records", [])
                if configs and configs[0].get("credential_state") == "revoked":
                    root, refs = "credential_revoked", [x["id"] for x in failed] + [configs[0]["id"]]
            elif families == {"server"}:
                if "list_deployments" not in observations:
                    return {"message": tool_call("list_deployments"), "input_tokens": 0, "output_tokens": 0}
                deployments = observations["list_deployments"].get("records", [])
                matches = [x for x in deployments if x.get("error_signature") and
                           any(log.get("error_signature") == x["error_signature"] for log in failed)]
                if matches:
                    root, refs = "deployment_regression", [x["id"] for x in failed] + [matches[-1]["id"]]
            if root == "unknown":
                reply = tool_call("finish", handoff())
            elif "search_runbook" not in observations:
                reply = tool_call("search_runbook", {"topic": root})
            else:
                refs += [x["id"] for x in observations["search_runbook"].get("records", [])]
                reply = tool_call("finish", {"root_cause": root, "action": ACTIONS[root],
                                             "status": "diagnosed", "evidence_ids": refs})
        return {"message": reply, "input_tokens": 0, "output_tokens": 0}


class Ollama:
    name = "ollama"

    def __init__(self, model, base_url="http://127.0.0.1:11434"):
        self.model = model
        self.base_url = base_url.rstrip("/")

    def chat(self, messages, schemas, timeout):
        data = json.dumps({"model": self.model, "messages": messages, "tools": schemas,
                           "stream": False, "options": {"temperature": 0, "num_predict": 2048}}).encode()
        request = urllib.request.Request(self.base_url + "/api/chat", data=data,
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ValueError("Model response too large")
        result = json.loads(raw)
        message = result["message"]
        if not isinstance(message, dict) or message.get("role") != "assistant":
            raise ValueError("Invalid model message")
        return {"message": message, "input_tokens": result.get("prompt_eval_count"),
                "output_tokens": result.get("eval_count")}


class ProviderHTTPError(Exception):
    def __init__(self, status):
        self.http_status = status
        super().__init__(f"Provider HTTP {status}")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class DeepSeek:
    name = "deepseek"

    def __init__(self, model="deepseek-v4-flash", api_key=None, base_url="https://api.deepseek.com"):
        self.model = model
        self._key = api_key or os.getenv("DEEPSEEK_API_KEY", "")
        if not self._key:
            raise ValueError("Set DEEPSEEK_API_KEY in .env or environment before using DeepSeek")
        parsed = urlsplit(base_url)
        if parsed.scheme != "https" or parsed.netloc != "api.deepseek.com" or parsed.path.rstrip("/") not in {"", "/v1"} or parsed.query or parsed.fragment:
            raise ValueError("DeepSeek adapter only sends credentials to https://api.deepseek.com")
        self.base_url = base_url.rstrip("/")
        names = ["DEEPSEEK_INPUT_USD_PER_MILLION", "DEEPSEEK_CACHE_USD_PER_MILLION", "DEEPSEEK_OUTPUT_USD_PER_MILLION"]
        self.pricing = [float(os.environ[x]) for x in names] if all(os.getenv(x) for x in names) else None
        if self.pricing and any(not math.isfinite(x) or x < 0 for x in self.pricing):
            raise ValueError("Invalid token pricing")

    def chat(self, messages, schemas, timeout):
        wire = copy.deepcopy(messages)
        for message in wire:
            message.pop("tool_name", None)
            for call in message.get("tool_calls", []):
                args = call["function"]["arguments"]
                if not isinstance(args, str):
                    call["function"]["arguments"] = json.dumps(args)
        payload = {"model": self.model, "messages": wire, "tools": schemas,
                   "tool_choice": "required", "stream": False, "temperature": 0,
                   "thinking": {"type": "disabled"}, "max_tokens": 2048}
        request = urllib.request.Request(self.base_url + "/chat/completions",
            data=json.dumps(payload).encode(), headers={"Content-Type": "application/json",
                                                       "Authorization": "Bearer " + self._key})
        try:
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout) as response:
                raw = response.read(2_000_001)
        except urllib.error.HTTPError as exc:
            raise ProviderHTTPError(exc.code) from None
        if len(raw) > 2_000_000:
            raise ValueError("Model response too large")
        result = json.loads(raw)
        original = result["choices"][0]["message"]
        message = {"role": "assistant", "content": original.get("content") or "",
                   "tool_calls": copy.deepcopy(original.get("tool_calls", []))}
        for call in message["tool_calls"]:
            if not isinstance(call.get("id"), str) or not call["id"]:
                raise ValueError("Missing tool call id")
            call["function"]["arguments"] = json.loads(call["function"]["arguments"])
        # The demo loop executes one tool per turn. DeepSeek may emit a parallel
        # batch; retain the first call so the next turn can observe its result.
        message["tool_calls"] = message["tool_calls"][:1]
        usage = result.get("usage") or {}
        input_tokens, output_tokens = usage.get("prompt_tokens"), usage.get("completion_tokens")
        hit = usage.get("prompt_cache_hit_tokens")
        cost = None
        if self.pricing and all(type(x) is int and x >= 0 for x in (input_tokens, output_tokens, hit)) and hit <= input_tokens:
            miss_price, hit_price, output_price = self.pricing
            cost = ((input_tokens - hit) * miss_price + hit * hit_price + output_tokens * output_price) / 1_000_000
        return {"message": message, "input_tokens": input_tokens, "output_tokens": output_tokens,
                "cache_hit_tokens": hit, "cost_usd": cost}


def make_provider(name, model=None, base_url=None):
    if name == "baseline":
        return Baseline()
    if name == "ollama":
        return Ollama(model or os.getenv("OLLAMA_MODEL", "qwen3:8b"),
                      base_url or os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434"))
    if name == "deepseek":
        return DeepSeek(model or os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"),
                        base_url=base_url or os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"))
    raise ValueError("Unknown provider")
