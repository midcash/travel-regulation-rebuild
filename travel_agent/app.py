"""Local FastAPI entry point; no credentials or raw supplier evidence in HTTP."""
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .runtime import Conversation
from .session_store import SessionStore, StorageError
from .settings import load_env

WEB = Path(__file__).with_name("web")


class Prompt(BaseModel):
    text: str = Field(min_length=1, max_length=8000)


def create_app(conversation=None):
    @asynccontextmanager
    async def lifespan(app):
        load_env()
        app.state.conversation = conversation or Conversation()
        current = app.state.conversation
        path = os.getenv("TRAVEL_SESSION_DB") or (str(Path(".local") / "sessions.sqlite3") if conversation is None else None)
        if path:
            current.store = SessionStore(path)
            state = current.store.load()
            if state:
                current.restore(state)
                current.durable = state
                current.recover()
            else:
                current.persist()
        yield
        await app.state.conversation.close()

    app = FastAPI(title="行旅", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=WEB), name="static")

    @app.get("/")
    async def index():
        return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/api/session")
    async def session():
        return app.state.conversation.snapshot()

    @app.post("/api/messages", status_code=202)
    async def prompt(body: Prompt):
        text = body.text.strip()
        if not text:
            raise HTTPException(422, "请输入差旅需求。")
        try:
            identifier = app.state.conversation.submit(text)
        except StorageError as error:
            raise HTTPException(503, str(error))
        except RuntimeError as error:
            raise HTTPException(409, "请开始新会话。" if str(error) == "context_lost" else "当前请求尚未结束，请稍候或取消。")
        return {"id": identifier}

    @app.post("/api/cancel")
    async def cancel():
        await app.state.conversation.cancel()
        return {"accepted": True}

    @app.post("/api/session/reset")
    async def reset():
        current = app.state.conversation
        if current.task and not current.task.done():
            raise HTTPException(409, "请等待当前请求结束。")
        replacement = Conversation(clock=current.clock, fake=current.fake, responses=current.responses,
            transport_http=current.transport_http, evidence_root=current.evidence_root, timeout=current.timeout,
            worker_factory=current.worker_factory)
        replacement.store = current.store
        try:
            replacement.persist()
        except StorageError as error:
            raise HTTPException(503, str(error))
        await current.close()
        app.state.conversation = replacement
        return replacement.snapshot()

    @app.get("/api/events")
    async def events(request: Request):
        async def stream():
            version = None
            while not await request.is_disconnected():
                current = app.state.conversation
                key = (current.session_id, current.revision)
                if key != version:
                    version = key
                    yield "data: " + json.dumps(current.snapshot(), ensure_ascii=False) + "\n\n"
                else:
                    yield ": keepalive\n\n"
                await asyncio.sleep(.25)
        return StreamingResponse(stream(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return app


app = create_app()
