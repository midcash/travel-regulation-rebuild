"""模型抽象层：mock 与真实 API 共用同一个 chat() 协议。

为什么要自己写一层：
    笔记里反复出现"外部框架把用户问题、历史消息和可用工具交给模型"（9.2 节）。
    要把这句话变成能 debug 的代码，就需要一个边界清晰的对象——它就是那个"模型"。
    有了它，ReAct 循环里哪一步是模型、哪一步是框架，在调试器里一眼就能分开。

两条约定（和主项目 travel_agent/providers.py 一致）：
  1. provider 是无状态的。MockLLM 的策略函数只读 messages 来重建"现在是第几回合"，
     不把进度记在实例上，这样跨进程、跨重跑的行为都可复现。
  2. tool_calls 的入参在 LLMResponse 里是 dict，只有 as_message() 序列化回对话历史时
     才变成 JSON 字符串——和真实 OpenAI 协议的形状保持一致。

默认不联网：AGENT_NOTES_LLM 不设置时走 mock，PyCharm 直接 Run 就能跑通。
想接真模型：设置环境变量 AGENT_NOTES_LLM=real 和 DEEPSEEK_API_KEY 即可。
"""
from __future__ import annotations

import json
import hashlib
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

Message = dict[str, Any]
ToolSchema = dict[str, Any]


def estimate_tokens(text: str) -> int:
    """粗糙的 token 估算：中文按 1 字 1 token，英文按 4 字符 1 token。

    只用于 demo 里显示"上下文膨胀了多少"，不追求和真实 tokenizer 对齐。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    other = len(text) - cjk
    return cjk + max(1, other // 4) if other else cjk


def count_message_tokens(messages: list[Message]) -> int:
    total = 0
    for msg in messages:
        total += 4  # 每条消息的固定开销
        total += estimate_tokens(str(msg.get("content") or ""))
        for call in msg.get("tool_calls") or []:
            fn = call.get("function", {})
            total += estimate_tokens(str(fn.get("name", "")))
            total += estimate_tokens(str(fn.get("arguments", "")))
    return total


# --------------------------------------------------------------------------
# 模型返回的结构
# --------------------------------------------------------------------------


@dataclass
class ToolCall:
    """模型要求调用某个工具，以及它给出的参数。"""

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            blob = json.dumps(self.arguments, sort_keys=True, ensure_ascii=False, default=str)
            # Python's built-in hash is process-randomized.  A stable id is
            # required for replay, idempotency and cross-worker tracing.
            digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]
            self.id = f"call_{self.name}_{digest}"


@dataclass
class LLMResponse:
    """一次模型调用的结果。要么是普通文本（最终答案），要么是工具调用请求。"""

    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = "mock"
    # 真实 provider 的原始响应，方便在调试器里看全貌
    raw: dict[str, Any] | None = None

    @property
    def wants_tool(self) -> bool:
        return bool(self.tool_calls)

    def as_message(self) -> Message:
        """转成可以塞回对话历史的 assistant 消息（OpenAI 协议形状）。"""
        msg: Message = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in self.tool_calls
            ]
        return msg


# 策略函数：给 messages 和可用工具，返回这一回合模型想干什么。
# 返回 str 表示最终答案，返回 ToolCall / list[ToolCall] 表示要调工具。
Policy = Callable[[list[Message], list[ToolSchema]], "LLMResponse | str | ToolCall | list[ToolCall] | None"]


def normalize(result: Any, model: str = "mock") -> LLMResponse:
    """把策略函数的松散返回值收敛成 LLMResponse。"""
    if isinstance(result, LLMResponse):
        return result
    if result is None:
        return LLMResponse(content="", model=model)
    if isinstance(result, str):
        return LLMResponse(content=result, model=model)
    if isinstance(result, ToolCall):
        return LLMResponse(tool_calls=[result], model=model)
    if isinstance(result, (list, tuple)):
        return LLMResponse(tool_calls=list(result), model=model)
    raise TypeError(f"策略函数返回了不支持的类型: {type(result)!r}")


class LLM(Protocol):
    name: str

    def chat(self, messages: list[Message], tools: list[ToolSchema]) -> LLMResponse: ...


# --------------------------------------------------------------------------
# 实现一：MockLLM —— 不联网，可复现，PyCharm 直接 Run
# --------------------------------------------------------------------------


class MockLLM:
    """用策略函数冒充模型。

    这是整个实验区最重要的一个设计：把"模型会怎么决策"写成一段你能读、能打断点的
    Python。真实模型是黑盒，但在学习循环结构时，你需要一个确定性的对手，
    否则每次 Run 的输出都不一样，断点根本没法看。
    """

    def __init__(self, policy: Policy, name: str = "mock-llm", model: str = "mock-1"):
        self.policy = policy
        self.name = name
        self.model = model
        self.calls = 0  # 被调用了多少次，等于循环转了几圈

    def chat(self, messages: list[Message], tools: list[ToolSchema] | None = None) -> LLMResponse:
        self.calls += 1
        response = normalize(self.policy(messages, list(tools or [])), model=self.model)
        if not response.model or response.model == "mock":
            response.model = self.model
        # 用估算值补上 token，让"预算"类实验有数可算
        response.input_tokens = response.input_tokens or count_message_tokens(messages)
        response.output_tokens = response.output_tokens or estimate_tokens(
            response.content + json.dumps([c.arguments for c in response.tool_calls], ensure_ascii=False)
        )
        return response


class ScriptedLLM:
    """按剧本吐结果，第几回合由 messages 里 assistant 消息的条数推导（无状态）。

    script 每一项可以是：
      - str                      -> 最终答案
      - ToolCall                 -> 一次工具调用
      - list[ToolCall]           -> 并行调用多个工具
      - Exception 实例           -> 直接抛出来（专门用来构造模型服务故障）
    剧本用完后就返回最后一项（如果最后一项是 str）或报错。
    """

    def __init__(self, script: list[Any], name: str = "scripted-llm", model: str = "scripted-1"):
        self.script = list(script)
        self.name = name
        self.model = model
        self.calls = 0

    def _turn(self, messages: list[Message]) -> int:
        return sum(1 for m in messages if m.get("role") == "assistant")

    def chat(self, messages: list[Message], tools: list[ToolSchema] | None = None) -> LLMResponse:
        self.calls += 1
        turn = self._turn(messages)
        if turn >= len(self.script):
            # 剧本用完了：用最后一条 str 作为终止答案，避免 demo 无限循环
            for item in reversed(self.script):
                if isinstance(item, str):
                    return normalize(item, self.model)
            raise RuntimeError(f"剧本只有 {len(self.script)} 条，第 {turn} 回合没有内容可返回")
        item = self.script[turn]
        if isinstance(item, BaseException):
            raise item
        response = normalize(item, self.model)
        response.input_tokens = response.input_tokens or count_message_tokens(messages)
        response.output_tokens = response.output_tokens or estimate_tokens(response.content)
        return response


class FlakyLLM:
    """给任意 LLM 套一层"会坏"的壳：按 plan 指定的回合抛异常。

    笔记 17.3 讲模型调用失败，第一类就是"模型服务短暂抖动"。要演示重试，
    就必须先有一个能稳定复现故障的模型——这正是这个类存在的理由。
    plan 形如 {1: TimeoutError("read timeout"), 2: TimeoutError("read timeout")}，
    键是第几次调用（从 1 开始）。
    """

    def __init__(self, inner: LLM, plan: dict[int, BaseException], name: str | None = None):
        self.inner = inner
        self.plan = dict(plan)
        self.name = name or f"flaky({inner.name})"
        self.calls = 0
        self.failures = 0

    def chat(self, messages: list[Message], tools: list[ToolSchema] | None = None) -> LLMResponse:
        self.calls += 1
        if self.calls in self.plan:
            self.failures += 1
            raise self.plan[self.calls]
        return self.inner.chat(messages, tools)


# --------------------------------------------------------------------------
# 实现二：真实模型（OpenAI 兼容 /chat/completions，DeepSeek 就是这套）
# --------------------------------------------------------------------------


class OpenAICompatLLM:
    """走 HTTP 的真实 provider，只用标准库，不引入新依赖。

    只有显式设置 AGENT_NOTES_LLM=real 时才会被构造，所以默认路径完全不联网。
    """

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        temperature: float = 0.0,
        timeout: float = 60.0,
    ):
        self.model = model or os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
        self.api_key = api_key or os.environ.get("DEEPSEEK_API_KEY", "")
        self.base_url = (base_url or os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")).rstrip("/")
        self.temperature = temperature
        self.timeout = timeout
        self.name = f"real({self.model})"
        self.calls = 0
        if not self.api_key:
            raise RuntimeError("AGENT_NOTES_LLM=real 需要 DEEPSEEK_API_KEY")

    def chat(self, messages: list[Message], tools: list[ToolSchema] | None = None) -> LLMResponse:
        self.calls += 1
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:  # 把 HTTP 错误也变成 Python 异常，方便上层分类
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc

        choice = body["choices"][0]["message"]
        calls: list[ToolCall] = []
        for raw_call in choice.get("tool_calls") or []:
            fn = raw_call.get("function", {})
            try:
                arguments = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                # 真实模型的经典翻车点：参数不是合法 JSON。原样留给上层去修。
                arguments = {"__raw__": fn.get("arguments")}
            calls.append(ToolCall(name=fn.get("name", ""), arguments=arguments, id=raw_call.get("id", "")))
        usage = body.get("usage") or {}
        return LLMResponse(
            content=choice.get("content") or "",
            tool_calls=calls,
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            model=body.get("model", self.model),
            raw=body,
        )


def build_llm(policy: Policy | None = None, model: str | None = None) -> LLM:
    """按环境变量选 provider。默认 mock —— PyCharm 里 Run 就能跑。"""
    mode = os.environ.get("AGENT_NOTES_LLM", "mock").strip().lower()
    if mode in {"real", "api", "deepseek"}:
        if os.environ.get("DEEPSEEK_API_KEY"):
            return OpenAICompatLLM(model=model)
        print("[warn] AGENT_NOTES_LLM=real 但环境里没有 DEEPSEEK_API_KEY，回退到 mock")
    if policy is None:
        raise ValueError("mock 模式必须提供 policy")
    return MockLLM(policy)
