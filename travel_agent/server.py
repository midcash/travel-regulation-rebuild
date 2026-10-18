"""Loopback-only demo server; bounded jobs, JSON-only same-origin mutations."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
from urllib.parse import urlsplit
import uuid

from .agent import plan_trip
from .domain import CHINA, PACKAGE, custom_scenario, normalize_request, scenario_by_id, scenarios
from .evaluation import evaluate
from .providers import make_provider
from .real_tools import REAL_SCHEMAS
from . import hil
from .human import HumanInputError
from .request_parser import RequestInputError, parse_natural_request, validate_request_dates


class Application:
    def __init__(self, store, provider, otel=None, *, clock=None):
        self.store, self.provider, self.otel = store, provider, otel
        self.clock = clock or (lambda: datetime.now(CHINA))
        self.jobs = {}
        self.lock = threading.Lock()
        self.busy = False
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="travel-planning")
        hil._setup(store)

    def prepare_scenario(self, body, tool_mode):
        if "request_text" in body and "request" in body:
            raise RequestInputError("请只选择自然语言输入或结构化输入中的一种。")
        if "request_text" not in body and "request" not in body:
            return scenario_by_id(body.get("scenario", "hangzhou-shanghai")), None

        captured_at = self.clock()
        if captured_at.tzinfo is None:
            captured_at = captured_at.replace(tzinfo=CHINA)
        else:
            captured_at = captured_at.astimezone(CHINA)
        request = (parse_natural_request(body["request_text"], captured_at)
                   if "request_text" in body else body["request"])
        scenario = custom_scenario(request)
        validate_request_dates(scenario["request"], captured_at)
        scenario["request_received_at"] = captured_at.isoformat(timespec="seconds")
        normalized = normalize_request(scenario["request"], tool_mode="real")
        mode = {"flight": "飞机", "train": "火车", "any": "不限"}[normalized.get("transport_mode", "any")]
        meeting_at = datetime.fromisoformat(normalized["meeting_at"])
        previous_day = "前一日抵达" if normalized.get("arrive_previous_day", True) else "不要求前一日抵达"
        nights = normalized.get("stay_nights", 1 if normalized.get("arrive_previous_day", True) else 0)
        summary = (f"按亚洲/上海时间 {captured_at.isoformat(timespec='minutes')} 解析："
                   f"{normalized['origin']} → {normalized['destination']} · {normalized['meeting_location']} · "
                   f"{meeting_at.strftime('%Y-%m-%d %H:%M')} · {mode} · {previous_day} · "
                   f"住宿 {nights} 晚；暂按 1 人 1 间房。")
        return scenario, summary

    def restore_provider(self, config):
        if (config["provider"], config["model"]) == (self.provider.name, self.provider.model):
            return self.provider
        if config["provider"] == "ollama":
            raise HumanInputError("请用原 Ollama 模型配置重启服务后继续。")
        return make_provider(config["provider"], model=config["model"], base_url=config.get("base_url"))

    def submit_hil(self, body, action, run_id=None):
        if action == "start":
            mode = body.get("tool_mode", "demo")
            if mode not in {"demo", "real"}:
                raise HumanInputError("Invalid tool mode")
            scenario, request_summary = self.prepare_scenario(body, mode)
            selected = body.get("provider", self.provider.name)
            if selected not in {"baseline", "deepseek", "ollama"} or (selected == "ollama" and self.provider.name != "ollama"):
                raise HumanInputError("Invalid provider")
            if mode == "real" and selected == "baseline":
                raise HumanInputError("真实工具模式需要真实模型。")
            provider = self.provider if selected == self.provider.name else make_provider(selected)
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
            if action == "start" and request_summary:
                job["request_summary"] = request_summary
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
        request_summary = None
        if kind == "run":
            tool_mode = body.get("tool_mode", "demo")
            if tool_mode not in {"demo", "real"}:
                raise ValueError("Invalid tool mode")
            scenario, request_summary = self.prepare_scenario(body, tool_mode)
        if selected not in {"baseline", "ollama", "deepseek"}:
            raise ValueError("Invalid provider")
        if selected == "ollama" and self.provider.name != "ollama":
            raise ValueError("Start server with --provider ollama to enable a real model")
        provider = self.provider if selected == self.provider.name else make_provider(selected)
        if kind == "run":
            if tool_mode == "real" and selected == "baseline":
                raise ValueError("真实工具模式需要选择 DeepSeek 或 Ollama 模型")
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
            job = {"id": identifier, "kind": kind, "status": "running"}
            if request_summary:
                job["request_summary"] = request_summary
            self.jobs[identifier] = job

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
        return dict(job)


def make_server(store, provider, port=8766, otel=None, *, clock=None):
    app = Application(store, provider, otel, clock=clock)

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
                captured_at = app.clock().astimezone(CHINA)
                example_date = (captured_at + timedelta(days=3)).date().isoformat()
                return self.send(200, {"real_tools": [s["function"]["name"] for s in REAL_SCHEMAS if s["function"]["name"] != "finish"], "ollama_enabled": provider.name == "ollama", "model": provider.model,
                    "example_meeting_at": example_date + "T09:00",
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
            except RequestInputError as exc:
                return self.send(400, {"error": str(exc)})
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
