"""Explicit paid acceptance helper; never imported by offline checks.

Normal scope: two prompts, three model requests, one read-only flight search.
Prints only normalized results and redacted model diagnostics. No automatic retries.
"""
import asyncio
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from travel_agent.runtime import Conversation, NodeWorker, FALLBACK_REPLIES, NO_ANSWER_TEXT
from travel_agent.settings import load_env
from travel_agent.supplier_wire import redact


class ObservedWorker(NodeWorker):
    def __init__(self):
        super().__init__()
        self.flight_requests = 0

    async def receive(self):
        event = await super().receive()
        if event["type"] == "tool_request" and event.get("name") == "search_flights":
            self.flight_requests += 1
        if event["type"] == "message_end" and event.get("error"):
            secrets = tuple(os.getenv(k, "") for k in ("DEEPSEEK_API_KEY", "TUNIU_API_KEY"))
            print(json.dumps(redact({"model_error": event["error"]}, secrets), ensure_ascii=False))
        return event


async def main():
    load_env()
    conversation = Conversation(worker_factory=ObservedWorker)
    try:
        for turn, prompt in enumerate(("我要去三亚。", "杭州，2026 年 11 月 20 日。")):
            conversation.submit(prompt)
            await conversation.task
            print(json.dumps(conversation.messages[-1], ensure_ascii=False))
            if conversation.run["status"] != "complete":
                return 1
            answer = conversation.messages[-1]["text"].strip()
            if not answer or answer in {NO_ANSWER_TEXT, *FALLBACK_REPLIES.values()}:
                return 1
            if turn == 0:
                if conversation.worker.flight_requests or conversation.tools.calls or conversation.run["results"]:
                    return 1
            elif (not conversation.worker.flight_requests or not conversation.tools.calls
                  or not any(result.get("status") == "ok" and (result.get("data") or {}).get("candidates")
                             for result in conversation.run["results"])):
                return 1
        return 0
    finally:
        await conversation.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
