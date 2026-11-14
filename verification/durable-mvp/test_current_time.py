"""Current-time tool and deterministic relative-date resolution against a real Node host."""
import json
import os
from datetime import date, timedelta
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.request

ROOT = Path(__file__).resolve().parents[2]


class Host:
    def __init__(self, folder, args=()):
        self.output = []
        ready = queue.Queue()
        env = {k: v for k, v in os.environ.items() if k.upper() in {
            'PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'PATHEXT', 'COMSPEC'}}
        self.process = subprocess.Popen(['node', str(ROOT / 'durable_mvp/server.mjs'),
            '--data', str(folder), '--port', '0', *args], cwd=ROOT, env=env,
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


def node_now():
    """Ask a fresh Node process for nowInDefaultZone() so the clock is independently sourced."""
    script = "import {nowInDefaultZone} from './calendar.mjs'; console.log(nowInDefaultZone());"
    result = subprocess.run(['node', '--input-type=module', '-'], input=script,
                            cwd=ROOT / 'durable_mvp', capture_output=True, text=True, encoding='utf-8')
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


def tool_result(state):
    return json.loads(next(m['text'] for m in reversed(state['messages']) if m['role'] == 'toolResult'))


class CurrentTimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='durable-mvp-now-')
        self.host = Host(self.temp.name)

    def tearDown(self):
        self.host.kill()
        self.temp.cleanup()

    def test_get_current_time_tool(self):
        before = node_now()
        self.host.prompt('/now', 'now-1')
        state = self.host.wait()
        after = node_now()
        result = tool_result(state)
        # Allow the clock to cross a minute boundary while the request is in flight.
        self.assertIn(result['now'][:16], {before[:16], after[:16]})
        self.assertEqual(result['timeZone'], 'Etc/GMT-8')
        self.assertEqual(result['today'], result['now'][:10])
        self.assertEqual(result['tomorrow'], (date.fromisoformat(result['today']) + timedelta(days=1)).isoformat())
        self.assertEqual(result['dayAfterTomorrow'],
                         (date.fromisoformat(result['today']) + timedelta(days=2)).isoformat())
        self.assertIn(result['weekday'], ['周日', '周一', '周二', '周三', '周四', '周五', '周六'])

    def test_resolve_when_pure(self):
        script = """
import {resolveWhen} from './calendar.mjs';
const at = value => Date.parse(value);
const base = at('2026-11-09T10:00:00+08:00');
const results = {
  today: resolveWhen({day: 'today', time: '20:10'}, base),
  tomorrow: resolveWhen({day: 'tomorrow', time: '08:00'}, base),
  dayAfterTomorrow: resolveWhen({day: 'dayAfterTomorrow', time: '09:30'}, base),
  offset: resolveWhen({offsetMinutes: 180}, base),
  absolute: resolveWhen({absolute: '2026-11-09T20:10:00+09:00'}, base),
  crossMonth: resolveWhen({day: 'tomorrow', time: '09:00'}, at('2026-01-31T23:00:00+08:00')),
  crossYear: resolveWhen({day: 'dayAfterTomorrow', time: '08:00'}, at('2026-12-31T23:30:00+08:00')),
  duration: resolveWhen({day: 'today', time: '20:10', durationMinutes: 90}, base),
  missingTime: resolveWhen({day: 'today'}, base),
  conflict: resolveWhen({day: 'today', date: '2026-11-09', time: '10:00'}, base),
};
console.log(JSON.stringify(results));
"""
        result = subprocess.run(['node', '--input-type=module', '-'], input=script,
                                cwd=ROOT / 'durable_mvp', capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = json.loads(result.stdout)
        self.assertEqual(rows['today'], {'ok': True, 'start': '2026-11-09T20:10:00+08:00'})
        self.assertEqual(rows['tomorrow'], {'ok': True, 'start': '2026-11-10T08:00:00+08:00'})
        self.assertEqual(rows['dayAfterTomorrow'], {'ok': True, 'start': '2026-11-11T09:30:00+08:00'})
        self.assertEqual(rows['offset'], {'ok': True, 'start': '2026-11-09T13:00:00+08:00'})
        self.assertEqual(rows['absolute'], {'ok': True, 'start': '2026-11-09T19:10:00+08:00'})
        self.assertEqual(rows['crossMonth'], {'ok': True, 'start': '2026-02-01T09:00:00+08:00'})
        self.assertEqual(rows['crossYear'], {'ok': True, 'start': '2027-01-02T08:00:00+08:00'})
        self.assertEqual(rows['duration'],
                         {'ok': True, 'start': '2026-11-09T20:10:00+08:00', 'end': '2026-11-09T21:40:00+08:00'})
        self.assertFalse(rows['missingTime']['ok'])
        self.assertIn('time', rows['missingTime']['missing'])
        self.assertFalse(rows['conflict']['ok'])

    def test_create_when_missing_time_needs_information(self):
        payload = {'title': '相对日程', 'when': {'day': 'today'}}
        self.host.prompt('/calendar ' + json.dumps(payload, ensure_ascii=False), 'when-missing-time')
        state = self.host.wait()
        result = tool_result(state)
        self.assertEqual(result['status'], 'needs_information')
        self.assertIn('time', result['missing'])
        self.assertEqual(result['spec'], {'day': 'today'})
        self.assertEqual(state['calendar']['events'], [])
        self.assertEqual(state['calendar']['pending'], {})

    def test_prompt_requires_tool(self):
        text = (ROOT / 'durable_mvp/server.mjs').read_text(encoding='utf-8')
        clock = [line for line in text.splitlines() if 'calendar-clock' in line]
        self.assertEqual(len(clock), 1)
        section = clock[0]
        for token in ['get_current_time', '禁止', 'when', '今天/明天/后天', '追问']:
            self.assertIn(token, section)

    def test_now_file_clock_consistency(self):
        fixed = '2026-11-09T12:34:56+08:00'
        clock_file = Path(self.temp.name, 'now.txt')
        clock_file.write_text(fixed, encoding='utf-8')
        self.host.kill()
        self.host = Host(self.temp.name, args=['--now-file', str(clock_file)])
        self.host.prompt('/now', 'now-fixed')
        result = tool_result(self.host.wait())
        self.assertTrue(result['now'].startswith('2026-11-09T12:34:56'), result['now'])
        self.assertEqual(result['today'], '2026-11-09')
        self.assertEqual(result['tomorrow'], '2026-11-10')
        self.assertEqual(result['dayAfterTomorrow'], '2026-11-11')

    def test_now_file_consistency_for_create_when(self):
        # The --now-file clock must drive create-by-when exactly like get_current_time,
        # not the real Date.now().
        fixed = '2026-11-09T12:34:56+08:00'
        clock_file = Path(self.temp.name, 'now-create.txt')
        clock_file.write_text(fixed, encoding='utf-8')
        self.host.kill()
        self.host = Host(self.temp.name, args=['--now-file', str(clock_file)])

        self.host.prompt('/now', 'now-fixed-create')
        now_result = tool_result(self.host.wait())
        self.assertTrue(now_result['now'].startswith('2026-11-09T12:34:56'), now_result['now'])

        payload = {'title': '注入时钟日程',
                   'when': {'day': 'tomorrow', 'time': '20:10', 'durationMinutes': 60}}
        self.host.prompt('/calendar ' + json.dumps(payload, ensure_ascii=False), 'when-fixed-create')
        result = tool_result(self.host.wait())
        self.assertEqual(result['status'], 'created', result)
        expected_start = (date.fromisoformat(now_result['today']) + timedelta(days=1)).isoformat() + 'T20:10:00+08:00'
        self.assertEqual(result['event']['start'], expected_start)
        self.assertEqual(result['event']['start'][:10], now_result['tomorrow'])
        self.assertEqual(result['event']['start'][:10],
                         (date.fromisoformat(now_result['today']) + timedelta(days=1)).isoformat())


if __name__ == '__main__':
    unittest.main()
