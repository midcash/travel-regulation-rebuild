"""Python-owned subprocess and SQLite evidence for the isolated Pi validation."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from contextlib import contextmanager
import json
import os
from pathlib import Path
import queue
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
WORKER = Path(__file__).with_name('worker.mjs')
SHANGHAI = timezone(timedelta(hours=8))


class Node:
    def __init__(self):
        self.temp = tempfile.TemporaryDirectory(prefix='travel-pi-step0-')
        env = os.environ.copy()
        env.update(HOME=self.temp.name, USERPROFILE=self.temp.name, APPDATA=self.temp.name,
                   LOCALAPPDATA=self.temp.name, PI_CODING_AGENT_DIR=self.temp.name,
                   PI_OFFLINE='1', STEP0_REPO=str(ROOT), STEP0_REAL_HOME=str(Path.home()),
                   NODE_NO_WARNINGS='1')
        self.process = subprocess.Popen(['node', str(WORKER)], cwd=self.temp.name, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding='utf-8', creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.queue = queue.Queue()
        self.events = []
        self.observer = None
        self.journal = None
        self.captured_at = None
        self.condition_version = 1
        self.stderr = []
        def read():
            try:
                for line in self.process.stdout:
                    self.queue.put(json.loads(line))
            finally:
                self.queue.put({'type': 'exit', 'code': self.process.poll()})
        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()
        def errors():
            for line in self.process.stderr:
                self.stderr.append(line)
        self.error_reader = threading.Thread(target=errors, daemon=True)
        self.error_reader.start()
        ready = self.receive(35)
        if ready['type'] != 'ready':
            raise RuntimeError('Node did not start: ' + ''.join(self.stderr)[-1000:])

    def send(self, op, **body):
        identifier = uuid.uuid4().hex
        if op in {'prompt', 'steer'} and self.journal:
            self.journal.message(body['text'], self.captured_at, self.condition_version, identifier)
        self.process.stdin.write(json.dumps({'op': op, 'id': identifier, 'conditionVersion': self.condition_version,
                                            'capturedAt': self.captured_at, **body}, ensure_ascii=False)+'\n')
        self.process.stdin.flush()
        return identifier

    def receive(self, timeout=120):
        try:
            item = self.queue.get(timeout=timeout)
        except queue.Empty as exc:
            raise TimeoutError('Node event timeout') from exc
        self.events.append(item)
        if self.observer:
            self.observer(item)
        return item

    def wait(self, identifier, tool=None, observe=None, timeout=180):
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            event = self.receive(max(.01, deadline-time.monotonic()))
            if observe:
                observe(event)
            if event['type'] == 'tool_request':
                result = tool(event) if tool else {'status': 'error', 'error': 'No configured fixture'}
                # tool callback MUST commit SQLite before this response is written.
                self.process.stdin.write(json.dumps({'op': 'tool_result', 'id': event['id'], 'result': result}, ensure_ascii=False)+'\n')
                self.process.stdin.flush()
            if event.get('id') == identifier and event['type'] in {'done', 'failed'}:
                if event['type'] == 'failed':
                    raise RuntimeError(event['error'])
                return event['result']
            if event['type'] == 'exit':
                raise RuntimeError('Node exited: '+''.join(self.stderr)[-1200:])
        raise TimeoutError('Node command timeout')

    def call(self, op, tool=None, observe=None, **body):
        return self.wait(self.send(op, **body), tool=tool, observe=observe)

    def close(self, kill=False):
        try:
            if self.process.poll() is None:
                if kill:
                    self.process.kill()
                else:
                    self.wait(self.send('shutdown'), timeout=10)
                self.process.wait(timeout=10)
        finally:
            if self.process.poll() is None:
                self.process.kill()
                self.process.wait(timeout=10)
            self.reader.join(timeout=3)
            self.error_reader.join(timeout=3)
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                stream.close()
            self.temp.cleanup()


class Journal:
    def __init__(self, path):
        self.path = Path(path)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS pi0_messages(id TEXT PRIMARY KEY, captured_at TEXT, version INTEGER, body TEXT);
                CREATE TABLE IF NOT EXISTS pi0_tools(id TEXT PRIMARY KEY, status TEXT, version INTEGER, args TEXT, result TEXT);
                CREATE TABLE IF NOT EXISTS pi0_entries(id TEXT PRIMARY KEY, body TEXT);
                CREATE TABLE IF NOT EXISTS pi0_state(id TEXT PRIMARY KEY, version INTEGER, body TEXT);
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path)
        try:
            with db:
                yield db
        finally:
            db.close()

    def message(self, text, captured_at, version=1, identifier=None):
        identifier = identifier or uuid.uuid4().hex
        assert datetime.fromisoformat(captured_at).utcoffset() == timedelta(hours=8)
        with self.connect() as db:
            db.execute('INSERT INTO pi0_messages VALUES(?,?,?,?)', (identifier, captured_at, version, text))
        return identifier

    def tool(self, event, operation, version=1, interrupt=False):
        with self.connect() as db:
            previous = db.execute('SELECT status,result,args FROM pi0_tools WHERE id=?', (event['id'],)).fetchone()
            args = json.dumps(event['args'], sort_keys=True, ensure_ascii=False)
            if previous:
                if previous[2] != args:
                    raise ValueError('Conflicting duplicate tool ID')
                if previous[0] == 'complete':
                    return json.loads(previous[1])
                raise ValueError('Unknown outcome; retry must use a new query ID')
            db.execute('INSERT INTO pi0_tools VALUES(?,?,?,?,NULL)', (event['id'], 'unknown', version, args))
        result = operation(event)
        if interrupt:
            raise InterruptedError('Supplier returned before SQLite result commit')
        result = {**result, 'query_id': event['id'], 'condition_version': version, 'query': event['args']}
        with self.connect() as db:
            db.execute('UPDATE pi0_tools SET status=?,result=? WHERE id=?', ('complete', json.dumps(result, ensure_ascii=False), event['id']))
        return result

    def save(self, entries):
        with self.connect() as db:
            for entry in entries:
                db.execute('INSERT OR REPLACE INTO pi0_entries VALUES(?,?)', (entry['id'], json.dumps(entry, ensure_ascii=False)))

    def load(self):
        with self.connect() as db:
            return [json.loads(r[0]) for r in db.execute('SELECT body FROM pi0_entries ORDER BY rowid')]

    def select(self, identifier, version, body):
        # Isolated contract: production must additionally bind candidate to observed evidence.
        with self.connect() as db:
            old = db.execute('SELECT version FROM pi0_state WHERE id=?', (identifier,)).fetchone()
            if old and old[0] > version:
                raise ValueError('Stale condition version')
            db.execute('INSERT OR REPLACE INTO pi0_state VALUES(?,?,?)', (identifier, version, json.dumps(body, ensure_ascii=False)))


def tool_schema(name='lookup'):
    return {'name': name, 'description': 'Query deterministic travel data; use the exact requested city.',
            'parameters': {'type': 'object', 'properties': {'city': {'type': 'string'}}, 'required': ['city'], 'additionalProperties': False}}


def summary(events):
    ends = [e for e in events if e['type'] == 'model_end']
    return {'model_calls': len([e for e in events if e['type'] == 'model_start']),
            'completed_requests': len(ends),
            'tool_calls': len([e for e in events if e['type'] == 'tool_request']),
            'text_deltas': sum(e['type'] == 'text_delta' for e in events),
            'thinking_deltas': sum(e['type'] == 'thinking_delta' for e in events),
            'thinking_chars': sum(e.get('thinkingLength', 0) for e in ends),
            'usage': {key: sum(e.get('usage', {}).get(key, 0) for e in ends)
                      for key in ('input', 'output', 'cacheRead', 'cacheWrite', 'totalTokens')},
            'usage_observed': all('usage' in e for e in ends) and bool(ends),
            'usage_complete': bool(ends) and len(ends)==sum(e['type']=='model_start' for e in events)
                              and all(e.get('stopReason') not in {'error', 'aborted'} for e in ends),
            'raw_usage': [e['usage'] for e in events if e['type'] == 'raw_usage'],
            'response_models': sorted({e['responseModel'] for e in events if e['type'] == 'raw_usage' and e.get('responseModel')}),
            'errors': [e.get('error', e.get('stopReason')) for e in ends if e.get('stopReason') in ('error', 'aborted')]}
