"""Real HTTP + Node + Durable + SQLite; only the model provider is synthetic."""
import json
import http.client
import os
from pathlib import Path
import queue
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[2]


class Host:
    def __init__(self, folder):
        self.output = []
        ready = queue.Queue()
        env = {k: v for k, v in os.environ.items() if k.upper() in {
            'PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'PATHEXT', 'COMSPEC'}}
        self.process = subprocess.Popen(['node', str(ROOT / 'durable_mvp/server.mjs'),
            '--data', str(folder), '--port', '0'], cwd=ROOT, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8')

        def read():
            for line in self.process.stdout:
                self.output.append(line)
                if line.startswith('DURABLE_MVP '):
                    ready.put(line.strip().split(' ', 1)[1])
            ready.put(None)
        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()
        try:
            self.url = ready.get(timeout=20)
            if not self.url:
                raise AssertionError(''.join(self.output))
        except BaseException:
            self.kill()
            raise

    def call(self, route, data=None, headers=None):
        request = urllib.request.Request(self.url + route,
            data=None if data is None else json.dumps(data).encode(),
            headers=headers or ({} if data is None else {'Content-Type': 'application/json'}))
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)

    def wait(self, condition=lambda s: not s['busy'], timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.call('/state')
            if condition(state):
                return state
            time.sleep(.05)
        raise AssertionError('State deadline: ' + repr(state))

    def prompt(self, text, request_id):
        return self.call('/prompt', {'text': text, 'requestId': request_id})

    def kill(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=10)
        self.reader.join(timeout=2)
        self.process.stdout.close()


class MVPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='durable-mvp-test-')
        self.host = Host(self.temp.name)

    def tearDown(self):
        self.host.kill()
        self.temp.cleanup()

    def restart(self):
        self.host.kill()
        self.host = Host(self.temp.name)

    def raw_post(self, content_type, body):
        host, port = self.host.url.removeprefix('http://').rsplit(':', 1)
        head = (f'POST /prompt HTTP/1.1\r\nHost: {host}:{port}\r\n'
                f'Content-Type: {content_type}\r\nContent-Length: {len(body)}\r\n'
                'Connection: keep-alive\r\n\r\n').encode()
        # read until EOF, so a host that leaves the socket open fails on the timeout
        with socket.create_connection((host, int(port)), timeout=10) as raw:
            raw.sendall(head + body)
            chunks = []
            while chunk := raw.recv(65536):
                chunks.append(chunk)
        return b''.join(chunks).decode()

    def test_note_restore_and_idempotent_retry(self):
        receipt = self.host.prompt('/note MVP笔记', 'note-1')
        before = self.host.wait()
        self.assertEqual(before['note'], 'MVP笔记')
        self.assertEqual(before['counts'], {'model': 2, 'tool': 1})
        self.assertEqual(self.host.prompt('/note MVP笔记', 'note-1'), receipt)
        self.assertEqual(self.host.wait()['counts'], before['counts'])
        self.restart()
        after = self.host.wait()
        self.assertEqual(after['messages'], before['messages'])
        self.assertEqual(after['note'], before['note'])
        self.assertEqual(after['counts'], {'model': 0, 'tool': 0})
        self.assertEqual(self.host.prompt('/note MVP笔记', 'note-1'), receipt)
        self.assertEqual(self.host.wait()['counts'], {'model': 0, 'tool': 0})
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.host.prompt('different', 'note-1')
        self.assertEqual(error.exception.code, 409)
        self.host.prompt('继续', 'next-1')
        self.assertIn('继续', self.host.wait()['messages'][-1]['text'])

    def test_interrupted_safe_tool_resumes(self):
        self.host.prompt('/slow 恢复完成', 'slow-1')
        self.host.wait(lambda s: s['counts']['tool'] == 1)
        self.restart()
        state = self.host.wait()
        self.assertEqual(state['counts'], {'model': 1, 'tool': 1})
        self.assertIn('恢复完成', state['messages'][-1]['text'])
        self.assertEqual(sum(m['role'] == 'user' for m in state['messages']), 1)

    def test_abort_and_continue(self):
        self.host.prompt('/slow 取消这轮', 'abort-1')
        self.host.wait(lambda s: s['counts']['tool'] == 1)
        self.host.call('/abort', {})
        self.assertFalse(self.host.wait()['busy'])
        self.host.prompt('下一轮', 'abort-2')
        self.assertIn('下一轮', self.host.wait()['messages'][-1]['text'])

    def test_tool_error_is_visible_and_conversation_continues(self):
        self.host.prompt('/fail', 'fail-1')
        state = self.host.wait()
        self.assertTrue(any(m['role'] == 'toolResult' and m['error'] for m in state['messages']))
        self.host.prompt('还能继续', 'fail-2')
        self.assertIn('还能继续', self.host.wait()['messages'][-1]['text'])

    def test_background_timer_survives_restart_and_abort(self):
        self.host.call('/timer', {})
        self.host.wait(lambda s: s['timers'] and s['timers'][0]['state']['status'] == 'sleeping')
        self.host.call('/abort', {})
        self.restart()
        state = self.host.wait(lambda s: s['timers'][0]['state']['status'] == 'terminal')
        self.assertEqual(state['timers'][0]['state']['outcome']['status'], 'completed')
        self.assertEqual(state['counts'], {'model': 0, 'tool': 0})
        self.restart()
        self.assertEqual(self.host.wait()['timers'], state['timers'])

    def test_read_only_poll_and_http_boundaries(self):
        before = self.host.wait()
        for _ in range(3):
            self.assertEqual(self.host.call('/state')['counts'], before['counts'])
        for headers, code in [({'Origin': 'https://evil.invalid', 'Content-Type': 'application/json'}, 403),
                              ({'Content-Type': 'text/plain'}, 415)]:
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.host.call('/prompt', {'text': 'test', 'requestId': 'test-1'}, headers)
            self.assertEqual(error.exception.code, code)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.host.prompt(' ', 'empty-1')
        self.assertEqual(error.exception.code, 400)
        self.assertEqual(self.host.call('/state')['messages'], before['messages'])

    def test_json_media_type_with_parameters_is_accepted(self):
        receipt = self.host.call('/prompt', {'text': '带参数类型', 'requestId': 'charset-1'},
                                 {'Content-Type': 'application/json; charset=utf-8'})
        self.assertIn('submissionId', receipt)
        self.assertIn('带参数类型', self.host.wait()['messages'][-1]['text'])

    def test_oversized_request_is_rejected_and_closes_connection(self):
        body = json.dumps({'text': 'x' * 20000, 'requestId': 'oversized-1'}).encode()
        response = self.raw_post('application/json', body)
        self.assertTrue(response.startswith('HTTP/1.1 413'), response[:100])
        self.assertIn('Connection: close', response)
        self.assertIn('Request too large', response)
        self.assertEqual(self.host.call('/state')['messages'], [])

    def test_unread_rejection_closes_connection(self):
        body = json.dumps({'text': 'x' * 20000, 'requestId': 'wrong-type-1'}).encode()
        response = self.raw_post('text/plain', body)
        self.assertTrue(response.startswith('HTTP/1.1 415'), response[:100])
        self.assertIn('Connection: close', response)
        self.assertEqual(self.host.call('/state')['messages'], [])

    def test_second_host_cannot_open_same_store(self):
        with self.assertRaisesRegex(AssertionError, 'already in use'):
            Host(self.temp.name)
        self.assertFalse(self.host.wait()['fatal'])

    def test_corrupt_lock_database_keeps_its_own_error(self):
        with tempfile.TemporaryDirectory(prefix='durable-mvp-corrupt-') as folder:
            Path(folder, 'host.lock.sqlite').write_bytes(b'not a sqlite database' * 256)
            with self.assertRaises(AssertionError) as error:
                Host(folder)
            message = str(error.exception)
            self.assertNotIn('already in use', message)
            self.assertIn('not a database', message)

    def test_chunked_utf8_is_not_corrupted(self):
        address = self.host.url.removeprefix('http://')
        connection = http.client.HTTPConnection(address, timeout=20)
        body = json.dumps({'text': '/note 分块中文', 'requestId': 'utf8-1'}, ensure_ascii=False).encode()
        # Each UTF-8 character is deliberately split over HTTP chunks.
        connection.request('POST', '/prompt', (body[i:i+1] for i in range(len(body))),
                           {'Content-Type': 'application/json'}, encode_chunked=True)
        response = connection.getresponse()
        self.assertEqual(response.status, 202, response.read())
        connection.close()
        self.assertEqual(self.host.wait()['note'], '分块中文')

if __name__ == '__main__':
    unittest.main()
