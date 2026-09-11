"""Loopback-only demo server; bounded jobs, JSON-only same-origin mutations."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
from urllib.parse import urlsplit
import uuid

from .agent import plan_trip
from .domain import PACKAGE, scenarios, scenario_by_id, custom_scenario
from .evaluation import evaluate
from .providers import make_provider
from .real_tools import REAL_SCHEMAS
from . import hil
from .human import HumanInputError


class Application:
    def __init__(self, store, provider, otel=None):
        self.store, self.provider, self.otel = store, provider, otel
        self.jobs = {}
        self.lock = threading.Lock()
        self.busy = False
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="travel-planning")
        hil._setup(store)

    def restore_provider(self, config):
        if (config["provider"], config["model"]) == (self.provider.name, self.provider.model):
            return self.provider
        if config["provider"] == "ollama":
            raise HumanInputError("请用原 Ollama 模型配置重启服务后继续。")
        return make_provider(config["provider"], model=config["model"], base_url=config.get("base_url"))

    def submit_hil(self, body, action, run_id=None):
        if action == "start":
            selected = body.get("provider", self.provider.name)
            if selected not in {"baseline", "deepseek", "ollama"} or (selected == "ollama" and self.provider.name != "ollama"):
                raise HumanInputError("Invalid provider")
            provider = self.provider if selected == self.provider.name else make_provider(selected)
            mode = body.get("tool_mode", "demo")
            if mode not in {"demo", "real"} or (mode == "real" and selected == "baseline"):
                raise HumanInputError("真实工具模式需要真实模型。")
            scenario = custom_scenario(body["request"]) if "request" in body else scenario_by_id(body.get("scenario", "hangzhou-shanghai"))
        elif action not in {"respond", "resume", "cancel"}:
            raise HumanInputError("Invalid HIL action")
        else:
            hil.get_trip(run_id, self.store)
        with self.lock:
            if self.busy:
                return None
            self.busy = True
            identifier = uuid.uuid4().hex
            if len(self.jobs) >= 100:
                self.jobs.pop(next(iter(self.jobs)))
            job = {"id": identifier, "kind": "hil", "status": "running"}
            self.jobs[identifier] = job

        def work():
            try:
                if action == "start":
                    result = hil.start_trip(scenario, provider, self.store, tool_mode=mode, otel=self.otel)
                elif action == "respond":
                    result = hil.respond_trip(run_id, self.store, self.restore_provider,
                        interaction_id=body["interaction_id"], state_version=body["state_version"],
                        response_id=body["response_id"], reply=body["reply"], otel=self.otel)
                elif action == "cancel":
                    result = hil.cancel_trip(run_id, self.store, self.restore_provider, state_version=body["state_version"], otel=self.otel)
                else:
                    result = hil.resume_trip(run_id, self.store, self.restore_provider, otel=self.otel)
                update = {"status": "completed", "result_id": result["run_id"], "execution_status": result["execution_status"]}
            except (HumanInputError, hil.HilConflict) as exc:
                update = {"status": "failed", "error": str(exc)}
            except Exception as exc:
                update = {"status": "failed", "error": type(exc).__name__}
            finally:
                with self.lock:
                    self.jobs[identifier].update(update)
                    self.busy = False
        self.executor.submit(work)
        return dict(job)

    def submit(self, body, kind):
        selected = body.get("provider", self.provider.name)
        if selected not in {"baseline", "ollama", "deepseek"}:
            raise ValueError("Invalid provider")
        if selected == "ollama" and self.provider.name != "ollama":
            raise ValueError("Start server with --provider ollama to enable a real model")
        provider = self.provider if selected == self.provider.name else make_provider(selected)
        if kind == "run":
            tool_mode = body.get("tool_mode", "demo")
            if tool_mode == "real" and selected == "baseline":
                raise ValueError("真实工具模式需要选择 DeepSeek 或 Ollama 模型")
            if tool_mode not in {"demo", "real"}:
                raise ValueError("Invalid tool mode")
            scenario = custom_scenario(body["request"]) if "request" in body else scenario_by_id(body.get("scenario", "hangzhou-shanghai"))
        else:
            split, repeats = body.get("split", "all"), body.get("repeats", 1)
            if split not in {"all", "dev", "test"} or type(repeats) is not int or not 1 <= repeats <= 3:
                raise ValueError("Invalid evaluation options")
        with self.lock:
            if self.busy:
                return None
            self.busy = True
            identifier = uuid.uuid4().hex
            if len(self.jobs) >= 100:
                self.jobs.pop(next(iter(self.jobs)))
            self.jobs[identifier] = {"id": identifier, "kind": kind, "status": "running"}

        def work():
            try:
                result = plan_trip(scenario, provider, self.store, otel=self.otel, tool_mode=tool_mode) if kind == "run" else evaluate(
                    self.store, provider, split, repeats, otel=self.otel)
                update = {"status": "completed", "result_id": result["trace_id"] if kind == "run" else result["id"]}
            except Exception as exc:
                update = {"status": "failed", "error": type(exc).__name__}
            with self.lock:
                self.jobs[identifier].update(update)
                self.busy = False
        self.executor.submit(work)
        return {"id": identifier, "kind": kind, "status": "running"}


def make_server(store, provider, port=8766, otel=None):
    app = Application(store, provider, otel)

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, *_):
            pass  # Access URLs can contain user data; deliberately omit them.

        def host_allowed(self):
            actual_port = self.server.server_address[1]
            return self.headers.get("Host") in {f"127.0.0.1:{actual_port}", f"localhost:{actual_port}"}

        def send(self, code, value, content_type="application/json; charset=utf-8"):
            body = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            if not self.host_allowed():
                return self.send(403, {"error": "Invalid host"})
            path = urlsplit(self.path).path
            assets = {"/": ("index.html", "text/html; charset=utf-8"),
                      "/app.js": ("app.js", "application/javascript; charset=utf-8"),
                      "/style.css": ("style.css", "text/css; charset=utf-8")}
            if path in assets:
                filename, content_type = assets[path]
                return self.send(200, (PACKAGE / "web" / filename).read_bytes(), content_type)
            if path == "/api/health":
                return self.send(200, {"status": "ok"})
            if path == "/api/config":
                return self.send(200, {"real_tools": [s["function"]["name"] for s in REAL_SCHEMAS if s["function"]["name"] != "finish"], "ollama_enabled": provider.name == "ollama", "model": provider.model,
                    "deepseek_enabled": bool(os.getenv("DEEPSEEK_API_KEY")),
                    "deepseek_model": provider.model if provider.name == "deepseek" else os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"),
                    "scenarios": [{"id": x["id"], "label": x["label"], "request": x["request"]} for x in scenarios()]})
            if path == "/api/runs":
                return self.send(200, store.list_runs())
            if path.startswith("/api/hil/runs/"):
                try:
                    return self.send(200, hil.get_trip(path.rsplit("/", 1)[-1], store))
                except KeyError:
                    return self.send(404, {"error": "HIL run not found"})
                except hil.HilConflict:
                    return self.send(409, {"error": "Incompatible HIL snapshot"})
            if path.startswith("/api/runs/"):
                result = store.get_run(path.rsplit("/", 1)[-1])
                return self.send(200 if result else 404, result or {"error": "Trace not found"})
            if path == "/api/evaluations":
                return self.send(200, store.list_evaluations())
            if path.startswith("/api/jobs/"):
                with app.lock:
                    result = dict(app.jobs.get(path.rsplit("/", 1)[-1], {}))
                return self.send(200 if result else 404, result or {"error": "Job not found"})
            return self.send(404, {"error": "Not found"})

        def do_POST(self):
            if not self.host_allowed():
                return self.send(403, {"error": "Invalid host"})
            expected_origin = "http://" + self.headers["Host"]
            if self.headers.get("Origin", expected_origin) != expected_origin or self.headers.get("X-Travel-Client") != "dashboard":
                return self.send(403, {"error": "Same-origin client header required"})
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                return self.send(415, {"error": "JSON required"})
            hil_parts = self.path.strip("/").split("/")
            is_hil = self.path == "/api/hil/runs" or (len(hil_parts) == 5 and hil_parts[:3] == ["api", "hil", "runs"] and hil_parts[4] in {"respond", "resume", "cancel"})
            if self.path not in {"/api/run", "/api/evaluate"} and not is_hil:
                return self.send(404, {"error": "Not found"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 8192:
                    raise ValueError("Invalid request size")
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError("JSON object required")
                if is_hil:
                    job = app.submit_hil(body, "start" if len(hil_parts) == 3 else hil_parts[4], None if len(hil_parts) == 3 else hil_parts[3])
                else:
                    job = app.submit(body, "run" if self.path == "/api/run" else "eval")
                return self.send(202 if job else 409, job or {"error": "已有任务运行中，请稍后重试。"})
            except KeyError:
                return self.send(404, {"error": "Run or required field not found"})
            except (ValueError, TypeError):
                return self.send(400, {"error": "Invalid options; check scenario, provider, split and repeats"})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.app = app
    return server


def serve(store, provider, port=8766, otel=None):
    server = make_server(store, provider, port, otel)
    print(f"Travel Agent Lab: http://127.0.0.1:{server.server_address[1]}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        server.app.executor.shutdown(wait=True)
