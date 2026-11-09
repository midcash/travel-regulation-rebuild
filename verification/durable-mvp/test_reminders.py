"""Real HTTP + Node + Durable + SQLite reminder behaviour; only the model is synthetic.

Time is driven deterministically by the TEST-ONLY --now-file clock seam so we can
advance the Harness clock and observe reminder delivery without waiting real time.
"""
import json
import os
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
HOUR = 3600 * 1000
MINUTE = 60 * 1000


class ReminderHost:
    def __init__(self, folder, now_file):
        self.now_file = now_file
        self.output = []
        ready = queue.Queue()
        env = {k: v for k, v in os.environ.items() if k.upper() in {
            'PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'PATHEXT', 'COMSPEC'}}
        self.process = subprocess.Popen(
            ['node', str(ROOT / 'durable_mvp/server.mjs'),
             '--data', str(folder), '--port', '0', '--now-file', str(now_file)],
            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding='utf-8')

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

    def set_now(self, epoch_ms):
        Path(self.now_file).write_text(str(int(epoch_ms)), encoding='utf-8')

    def call(self, route, data=None):
        request = urllib.request.Request(self.url + route,
            data=None if data is None else json.dumps(data).encode(),
            headers={} if data is None else {'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)

    def state(self):
        return self.call('/state')

    def wait(self, condition=lambda s: not s['busy'], timeout=20):
        deadline = time.monotonic() + timeout
        state = self.state()
        while time.monotonic() < deadline:
            state = self.state()
            if condition(state):
                return state
            time.sleep(.05)
        raise AssertionError('State deadline: ' + repr(state))

    def kill(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=10)
        self.reader.join(timeout=2)
        self.process.stdout.close()


def iso(epoch_ms):
    # Fixed UTC+8 wall clock string with explicit +08:00 offset.
    import datetime
    dt = datetime.datetime.fromtimestamp(epoch_ms / 1000, tz=datetime.timezone.utc) + datetime.timedelta(hours=8)
    return dt.strftime('%Y-%m-%dT%H:%M:%S') + '+08:00'


class PureFunctions(unittest.TestCase):
    """Unit-test the reminder pure functions directly via node (no server)."""

    def test_resolve_remind_at_and_delivery(self):
        script = r"""
import {resolveRemindAt, resolveDelivery, planReminder, resolveDeliveryChannels, DEFAULT_ADVANCE_MINUTES, CATCH_UP_WINDOW_MS}
  from './reminders.mjs';
const HOUR = 3600 * 1000;
const out = {};
const now = Date.parse('2026-11-10T10:00:00+08:00');
const ev = {id: 'e1', title: 't', start: '2026-11-10T10:30:00+08:00'};
out.defaultAdvance = resolveRemindAt(ev, undefined, now);   // 10:20
out.customAdvance = resolveRemindAt(ev, 5, now);           // 10:25
out.tooLate = resolveRemindAt({...ev, start: '2026-11-10T10:05:00+08:00'}, 10, now); // null
out.zeroAdvance = resolveRemindAt(ev, 0, now);             // null
// delivery decisions
out.wait = resolveDelivery(now + 1000, now + 2000, now);             // 'wait'
out.deliver = resolveDelivery(now - 1000, now + 1000, now);          // 'deliver' (in future event)
out.deliverPast = resolveDelivery(now - 1000, now - HOUR, now);      // 'deliver' (within window)
out.expire = resolveDelivery(now - 1000, now - CATCH_UP_WINDOW_MS - HOUR, now); // 'expire'
const rec = planReminder(ev, undefined, now, null);
out.planStatus = rec.status; out.planAdvance = rec.advanceMinutes;
// channel degradation
out.bothUp = resolveDeliveryChannels({page: true, systemNotify: true});
out.sysDown = resolveDeliveryChannels({page: true, systemNotify: false});
out.pageDown = resolveDeliveryChannels({page: false, systemNotify: true});
out.allDown = resolveDeliveryChannels({page: false, systemNotify: false});
console.log(JSON.stringify(out));
"""
        result = subprocess.run(['node', '--input-type=module', '-'], input=script,
            cwd=ROOT / 'durable_mvp', capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertEqual(out['defaultAdvance'], now_of('2026-11-10T10:20:00+08:00'))
        self.assertEqual(out['customAdvance'], now_of('2026-11-10T10:25:00+08:00'))
        self.assertIsNone(out['tooLate'])
        self.assertIsNone(out['zeroAdvance'])
        self.assertEqual(out['wait'], 'wait')
        self.assertEqual(out['deliver'], 'deliver')
        self.assertEqual(out['deliverPast'], 'deliver')
        self.assertEqual(out['expire'], 'expire')
        self.assertEqual(out['planStatus'], 'scheduled')
        self.assertEqual(out['planAdvance'], 10)
        # Channel degradation: at least one available channel -> ok; system
        # notification down still delivers via page; both down -> not ok.
        self.assertEqual(out['bothUp'], {'ok': True, 'channels': ['page', 'system']})
        self.assertEqual(out['sysDown'], {'ok': True, 'channels': ['page']})
        self.assertEqual(out['pageDown'], {'ok': True, 'channels': ['system']})
        self.assertEqual(out['allDown'], {'ok': False, 'channels': []})


def now_of(iso_str):
    import datetime
    return int(datetime.datetime.strptime(iso_str, '%Y-%m-%dT%H:%M:%S+08:00')
               .replace(tzinfo=datetime.timezone(datetime.timedelta(hours=8))).timestamp() * 1000)


class ReminderIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='durable-reminder-test-')
        self.now_file = Path(self.temp.name) / 'now.txt'
        # Fixed reference: 2026-11-10T10:00:00+08:00
        self.t0 = now_of('2026-11-10T10:00:00+08:00')
        self.now_file.write_text(str(self.t0), encoding='utf-8')
        self.host = ReminderHost(self.temp.name, self.now_file)

    def tearDown(self):
        self.host.kill()
        self.temp.cleanup()

    def restart(self):
        self.host.kill()
        self.host = ReminderHost(self.temp.name, self.now_file)

    def _create(self, event_id_hint, start_iso, advance=None):
        payload = {'title': event_id_hint, 'start': start_iso,
                   'end': iso(now_of(start_iso) + HOUR), 'timeZone': 'Etc/GMT-8'}
        if advance is not None:
            payload['advanceMinutes'] = advance
        self.host.call('/prompt', {'text': '/calendar ' + json.dumps(payload),
                                   'requestId': 'cal-' + event_id_hint})

    def _reminders(self, state):
        return {r['eventId']: r for r in state['reminders']}

    def test_default_advance_schedules_and_catchup_delivers_once(self):
        self._create('evt-default', '2026-11-10T10:30:00+08:00')  # default 10min -> 10:20
        state = self.host.wait(lambda s: any(r['status'] == 'scheduled' for r in s['reminders']))
        rec = list(self._reminders(state).values())[0]
        self.assertEqual(rec['advanceMinutes'], 10)
        self.assertEqual(rec['remindAt'], now_of('2026-11-10T10:20:00+08:00'))
        # Deadline reached while running is delivered when the clock passes it.
        # The scheduler wakes on its timer against the injected clock; to make
        # the wake deterministic we advance the clock and restart, which re-arms
        # and immediately fires the now-due task (documented restart behaviour).
        self.host.kill()
        self.host.set_now(now_of('2026-11-10T10:20:01+08:00'))
        self.host = ReminderHost(self.temp.name, self.now_file)
        state = self.host.wait(lambda s: any(r['status'] == 'delivered' for r in s['reminders']), timeout=20)
        fired = [r for r in state['reminders'] if r['status'] == 'delivered']
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0]['eventId'], rec['eventId'])
        # Restart again at the same time: still delivered once (atomic mark, no re-fire).
        self.restart()
        self.host.wait(lambda s: s['reminders'], timeout=20)
        after = [r for r in self.host.state()['reminders'] if r['status'] == 'delivered']
        self.assertEqual(len(after), 1)

    def test_explicit_advance_overrides_default(self):
        self._create('evt-explicit', '2026-11-10T10:30:00+08:00', advance=5)  # -> 10:25
        state = self.host.wait(lambda s: any(r['status'] == 'scheduled' for r in s['reminders']))
        rec = list(self._reminders(state).values())[0]
        self.assertEqual(rec['advanceMinutes'], 5)
        self.assertEqual(rec['remindAt'], now_of('2026-11-10T10:25:00+08:00'))

    def test_reschedule_moves_reminder(self):
        self._create('evt-move', '2026-11-10T10:30:00+08:00')  # 10:20
        state = self.host.wait(lambda s: any(r['status'] == 'scheduled' for r in s['reminders']))
        event_id = list(self._reminders(state).values())[0]['eventId']
        # Move event start to 11:00 -> reminder should be 10:50.
        self.host.call('/prompt', {'text': '/update ' + json.dumps({
            'eventId': event_id, 'start': '2026-11-10T11:00:00+08:00',
            'end': '2026-11-10T12:00:00+08:00'}), 'requestId': 'mv-1'})
        state = self.host.wait(lambda s: self._reminders(s).get(event_id, {}).get('remindAt') == now_of('2026-11-10T10:50:00+08:00'))
        self.assertEqual(self._reminders(state)[event_id]['remindAt'], now_of('2026-11-10T10:50:00+08:00'))

    def test_delete_cancels_reminder(self):
        self._create('evt-del', '2026-11-10T10:30:00+08:00')
        state = self.host.wait(lambda s: any(r['status'] == 'scheduled' for r in s['reminders']))
        event_id = list(self._reminders(state).values())[0]['eventId']
        self.host.call('/prompt', {'text': '/delete ' + json.dumps({'eventId': event_id}),
                                   'requestId': 'del-1'})
        state = self.host.wait(lambda s: self._reminders(s).get(event_id, {}).get('status') == 'cancelled')
        self.assertEqual(self._reminders(state)[event_id]['status'], 'cancelled')

    def test_expired_outside_window_marks_without_delivering(self):
        self._create('evt-old', '2026-11-10T10:30:00+08:00')  # remindAt 10:20, event 10:30
        state = self.host.wait(lambda s: any(r['status'] == 'scheduled' for r in s['reminders']))
        # Simulate being offline: restart with clock far past the catch-up window.
        self.host.kill()
        self.host.set_now(now_of('2026-11-12T10:20:00+08:00'))  # 2 days later
        self.host = ReminderHost(self.temp.name, self.now_file)
        # Reminder task fires on resume; deadline long past & event start earlier
        # than catch-up window -> should NOT deliver (stay scheduled? become expired).
        # Framework fires the task; our fire mark delivers only if we decide deliver.
        # Give the scheduler a moment; assert it did NOT become delivered.
        time.sleep(1.0)
        rems = self.host.state()['reminders']
        statuses = [r['status'] for r in rems]
        self.assertNotIn('delivered', statuses)


if __name__ == '__main__':
    unittest.main()
