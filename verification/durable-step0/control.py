"""Disposable process fixture. Python owns the locked invocation ledger, not session state."""
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time

HERE = Path(__file__).resolve().parent


class Ledger:
    def __init__(self, root):
        self.path = root / "calls.jsonl"
        self.lock = threading.Lock()
        self.path.touch(exist_ok=False)

    def append(self, record):
        with self.lock, self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            stream.flush()

    def read(self, kind=None):
        with self.lock:
            rows = [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()]
        return rows if kind is None else [row for row in rows if row["kind"] == kind]

    def counts(self):
        rows = self.read()
        return tuple(sum(row["kind"] == kind for row in rows) for kind in ("model", "tool"))


class Node:
    def __init__(self, root, ledger, **options):
        self.root, self.ledger = root, ledger
        self.events, self.replies = [], queue.Queue()
        self.send_lock = threading.Lock()
        self.gates = {}
        self.threads = []
        self.serial = 0
        self.log = (root / ("node-" + str(time.time_ns()) + ".log")).open("w", encoding="utf-8")
        # No inherited model credentials or dotenv. Windows needs these OS/runtime paths.
        env = {key: value for key, value in os.environ.items() if key.upper() in {
            "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATHEXT", "COMSPEC"}}
        env.update({"HOME": str(root), "USERPROFILE": str(root), "NO_PROXY": "*"})
        self.process = subprocess.Popen([shutil.which("node") or "node", str(HERE / "worker.mjs")],
            cwd=root, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log,
            text=True, encoding="utf-8", bufsize=1)
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        try:
            self.call("init", **options)
        except BaseException:
            self.kill()
            raise

    def hold(self, name):
        self.gates[name] = threading.Event()

    def release(self, name):
        self.gates[name].set()

    def _read(self):
        for line in self.process.stdout:
            event = json.loads(line)
            self.events.append(event)
            if event.get("rpc"):
                thread = threading.Thread(target=self._peer, args=(event,), daemon=True)
                self.threads.append(thread)
                thread.start()
            else:
                self.replies.put(event)
        self.replies.put({"exit": True})

    def _peer(self, event):
        kind = event["kind"]
        if kind in ("model", "tool", "timer", "network"):
            self.ledger.append({key: value for key, value in event.items() if key != "rpc"})
        gate = self.gates.get(event.get("gate", ""))
        if gate is not None:
            gate.wait(45)
        result = {"flight": "OFFLINE-101", "price": None, "queriedAt": "2026-10-29T10:00:00+08:00"}
        try:
            self.send({"rpcResult": event["rpc"], "result": result})
        except (BrokenPipeError, OSError, ValueError):
            pass

    def send(self, value):
        with self.send_lock:
            self.process.stdin.write(json.dumps(value, ensure_ascii=False) + "\n")
            self.process.stdin.flush()

    def begin(self, op, **values):
        self.serial += 1
        self.send({"id": self.serial, "op": op, **values})
        return self.serial

    def result(self, serial, timeout=25):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            event = self.replies.get(timeout=max(.01, deadline - time.monotonic()))
            if event.get("exit"):
                raise AssertionError("Node exited; inspect this test's temporary stderr")
            if event.get("id") == serial:
                if "error" in event:
                    raise AssertionError(event["error"])
                return event["result"]
        raise AssertionError("Node response timed out")

    def call(self, op, **values):
        return self.result(self.begin(op, **values))

    def until(self, predicate, timeout=25):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            if self.process.poll() is not None:
                raise AssertionError("Node exited before the observation")
            time.sleep(.01)
        raise AssertionError("Observation timed out")

    def kill(self):
        if self.process.poll() is None:
            if os.name == "nt":
                command = ["taskkill", "/F", "/PID", str(self.process.pid)]
            else:
                command = ["kill", "-9", str(self.process.pid)]
            result = subprocess.run(command, capture_output=True, text=True)
            assert result.returncode == 0, result.stderr
            self.process.wait(timeout=10)
            assert self.process.returncode != 0, "Must die forcibly, without a close command"
            self.ledger.append({"kind": "kill", "command": command, "returncode": self.process.returncode})
        for gate in self.gates.values():
            gate.set()
        for thread in self.threads:
            thread.join(timeout=2)
        self.process.stdin.close()
        self.reader.join(timeout=2)
        self.process.stdout.close()
        self.log.close()
