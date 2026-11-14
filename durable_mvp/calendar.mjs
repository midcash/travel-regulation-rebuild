import {randomUUID} from 'node:crypto';
import {Type} from '@earendil-works/pi-ai';
import {defineDoc, defineEntry, defineTool} from '@earendil-works/pi-durable';

export const Calendar = defineDoc({kind: 'mvp.calendar', version: 1, scope: 'conversation',
  history: 'rewindable', fork: 'asOf', initial: () => ({events: [], operations: {}, pending: {}}), checkpointWhen: () => false});
const Updated = defineEntry('mvp.calendar-updated');
const Deleted = defineEntry('mvp.calendar-deleted');
const Created = defineEntry('mvp.calendar-created');
const output = value => ({output: [{type: 'text', text: JSON.stringify(value)}]});
// IANA's Etc/GMT sign is reversed: Etc/GMT-8 is the fixed UTC+8 used across the project.
export const DEFAULT_TIME_ZONE = 'Etc/GMT-8';
// Test-only clock override: the production default stays Date.now(). Tests may pin the
// clock through the server's --now-file argument without changing production behavior.
let nowProvider = () => Date.now();
export function setNowProvider(provider) {
  nowProvider = provider;
}
// Prompts resolve relative dates in DEFAULT_TIME_ZONE, so "now" must carry the same fixed +08:00 offset:
// a UTC timestamp would name the previous calendar date between 16:00 and 24:00 UTC.
export function nowInDefaultZone(ms = nowProvider()) {
  return new Date(ms + 8 * 3600 * 1000).toISOString().replace('Z', '+08:00');
}
// Require explicit offsets; reject invalid dates instead of silently normalizing them.
export function instant(value) {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(Z|[+-]\d{2}:\d{2})$/.test(value)) throw new Error('请提供含时区偏移的完整日期时间');
  const date = value.slice(0, 10);
  if (new Date(date).toISOString().slice(0, 10) !== date || !Number.isFinite(Date.parse(value)) ||
      Number(value.slice(11, 13)) > 23 || Number(value.slice(14, 16)) > 59 || Number(value.slice(17, 19)) > 59) throw new Error('日期时间无效');
  return Date.parse(value);
}
export function range(start, end) {
  const a = instant(start), b = instant(end);
  if (b <= a) throw new Error('结束时间必须晚于开始时间');
  return [a, b];
}
export function inRange(events, start, end) {
  const [a, b] = range(start, end);
  return events.filter(e => Date.parse(e.start) < b && Date.parse(e.end) > a);
}
const pad2 = value => String(value).padStart(2, '0');
// All resolved instants use the project-wide fixed +08:00 offset and drop milliseconds so
// they pass instant(); relative dates are computed from the UTC+8 wall clock, never UTC.
const formatInstant = ms => nowInDefaultZone(ms).replace(/\.\d{3}\+08:00$/, '+08:00');
const dateOf = ms => nowInDefaultZone(ms).slice(0, 10);
// Real Date arithmetic handles month/year boundaries; no hand-written carry logic.
function addDays(date, days) {
  const [year, month, day] = date.split('-').map(Number);
  const target = new Date(Date.UTC(year, month - 1, day + days));
  return `${target.getUTCFullYear()}-${pad2(target.getUTCMonth() + 1)}-${pad2(target.getUTCDate())}`;
}
const MAX_MINUTES = 525600; // one year, keeps arithmetic inside the representable range
const DAY_OFFSETS = {today: 0, tomorrow: 1, dayAfterTomorrow: 2};
// Turn the structured relative-time spec emitted by the model into absolute +08:00 instants.
// The function is pure apart from the injected nowMs so it can be unit tested directly.
export function resolveWhen(spec, nowMs = Date.now(), timeZone = DEFAULT_TIME_ZONE) {
  if (!spec || typeof spec !== 'object' || Array.isArray(spec)) return {ok: false, reason: '时间参数无效'};
  let durationMs;
  if (spec.durationMinutes !== undefined) {
    if (!Number.isInteger(spec.durationMinutes) || spec.durationMinutes <= 0 || spec.durationMinutes > MAX_MINUTES)
      return {ok: false, reason: 'durationMinutes 无效'};
    durationMs = spec.durationMinutes * 60000;
  }
  let start;
  if (spec.offsetMinutes !== undefined) {
    if (!Number.isInteger(spec.offsetMinutes) || Math.abs(spec.offsetMinutes) > MAX_MINUTES)
      return {ok: false, reason: 'offsetMinutes 无效'};
    start = formatInstant(nowMs + spec.offsetMinutes * 60000);
  } else if (spec.absolute !== undefined) {
    try {
      // A bare local datetime is interpreted in the project's fixed UTC+8 zone.
      const value = typeof spec.absolute === 'string' && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$/.test(spec.absolute)
        ? spec.absolute + '+08:00' : spec.absolute;
      start = formatInstant(instant(value));
    } catch {
      return {ok: false, reason: '时间格式无效'};
    }
  } else {
    if (spec.day !== undefined && spec.date !== undefined) return {ok: false, reason: '日期指定冲突'};
    let date;
    if (spec.day !== undefined) {
      const offset = DAY_OFFSETS[spec.day];
      if (offset === undefined) return {ok: false, reason: '日期格式无效'};
      date = addDays(dateOf(nowMs), offset);
    } else if (spec.date !== undefined) {
      const parsed = typeof spec.date === 'string' ? Date.parse(spec.date) : NaN;
      if (!Number.isFinite(parsed) || new Date(parsed).toISOString().slice(0, 10) !== spec.date)
        return {ok: false, reason: '日期格式无效'};
      date = spec.date;
    } else {
      return {ok: false, missing: ['day'], reason: '请向用户追问日期'};
    }
    if (spec.time === undefined) return {ok: false, missing: ['time'], reason: '请向用户追问具体时刻'};
    if (typeof spec.time !== 'string' || !/^([01]\d|2[0-3]):[0-5]\d$/.test(spec.time))
      return {ok: false, reason: '时间格式无效'};
    start = `${date}T${spec.time}:00+08:00`;
  }
  return durationMs === undefined ? {ok: true, start} : {ok: true, start, end: formatInstant(Date.parse(start) + durationMs)};
}
export function calendarTools(event) {
  const write = (updating = false) => defineTool({name: updating ? 'update_calendar_event' : 'create_calendar_event',
    description: (updating ? 'Update ONE saved event by eventId from list_calendar_events; omitted fields retain saved values. Never create a replacement event for an edit.' : 'Create ONE NEW event; never use this tool to edit a saved event or pass eventId.') + ` Provide title, start, end (explicit offsets) for creation; optional IANA timeZone defaults to ${DEFAULT_TIME_ZONE} (fixed UTC+8). Ask for missing creation fields. On needs_confirmation use the page confirmation button. Only status created or updated means success. confirmationId retries a pending operation; only server-recorded user confirmation authorizes overlap. No recurrence/reminders.`,
    parameters: Type.Object({...(updating ? {eventId: Type.Optional(Type.String())} : {}), title: Type.Optional(Type.String({maxLength: 200})), start: Type.Optional(Type.String()),
      end: Type.Optional(Type.String()), timeZone: Type.Optional(Type.String()),
      when: Type.Optional(Type.Object({day: Type.Optional(Type.String()), date: Type.Optional(Type.String()),
        absolute: Type.Optional(Type.String()), time: Type.Optional(Type.String()),
        offsetMinutes: Type.Optional(Type.Number()), durationMinutes: Type.Optional(Type.Number())})),
      confirmationId: Type.Optional(Type.String())}), replay: 'safe',
    execute: async (input, api, context) => {
      event('tool');
      if (!updating && Object.hasOwn(input, 'eventId')) throw new Error('创建工具不接受 eventId；修改请使用 update_calendar_event');
      if (input.when !== undefined) {
        const resolved = resolveWhen(input.when, nowProvider(), DEFAULT_TIME_ZONE);
        if (!resolved.ok) return output({status: 'needs_information', missing: resolved.missing ?? [],
          reason: resolved.reason, spec: input.when});
        if (input.start !== undefined && input.start !== resolved.start) {
          const given = Date.parse(input.start), expected = Date.parse(resolved.start);
          if (!(Number.isFinite(given) && given === expected))
            return output({status: 'needs_information', missing: [], reason: 'start 与 when 冲突', spec: input.when});
        }
        input = {...input, start: resolved.start};
        if (resolved.end !== undefined) input.end = resolved.end;
      }
      return api.commit(async tx => {
        const doc = await tx.doc(Calendar, api.conversationId);
        if (Object.hasOwn(doc.operations, api.callId)) return output(doc.operations[api.callId]);
        if (input.confirmationId && doc.pending[input.confirmationId]?.result) return output(doc.pending[input.confirmationId].result);
        if (!updating && !input.confirmationId && doc.events.length >= 200) throw new Error('MVP 日程上限为 200 条');
        let candidate, pending, original;
        const updateId = input.confirmationId ? doc.pending[input.confirmationId]?.updateId : (updating ? input.eventId : undefined);
        if (updating && !input.confirmationId && !updateId) return output({status: 'needs_information', missing: ['eventId']});
        if (updateId) {
          original = doc.events.find(e => e.id === updateId);
          if (!original) {
            const result = {status: 'not_found', eventId: updateId};
            if (input.confirmationId) doc.pending[input.confirmationId].result = result;
            doc.operations[api.callId] = result;
            return output(result);
          }
        }
        if (input.confirmationId) {
          pending = doc.pending[input.confirmationId];
          if (!pending) throw new Error('待确认日程不存在');
          if (pending.result) return output(pending.result);
          if (!pending.authorized) return output({status: 'needs_confirmation', confirmationId: input.confirmationId, event: pending.event});
          if (updateId && JSON.stringify(original) !== JSON.stringify(pending.original)) {
            const result = {status: 'stale', message: '原日程已改变，请重新查询并修改'};
            pending.result = result;
            doc.operations[api.callId] = result;
            return output(result);
          }
          candidate = pending.event;
          const conflicts = inRange(doc.events.filter(e => e.id !== updateId), candidate.start, candidate.end);
          if (conflicts.some(e => !(pending.conflicts ?? []).some(old => old.id === e.id && old.start === e.start && old.end === e.end))) {
            pending.authorized = false;
            pending.conflicts = conflicts;
            const result = {status: 'needs_confirmation', confirmationId: input.confirmationId, event: candidate, conflicts, message: '出现新的冲突，请用户重新确认'};
            doc.operations[api.callId] = result;
            return output(result);
          }
        } else {
          input = {...original, ...input, timeZone: input.timeZone || original?.timeZone || DEFAULT_TIME_ZONE};
          const missing = ['title', 'start', 'end'].filter(key => !input[key]?.trim());
          if (missing.length) return output({status: 'needs_information', missing, message: '请向用户追问缺少的信息'});
          range(input.start, input.end);
          const formatter = new Intl.DateTimeFormat('sv-SE', {timeZone: input.timeZone, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23'});
          for (const value of [input.start, input.end]) {
            if (formatter.format(new Date(value)).replace(' ', 'T') !== value.slice(0, 19)) throw new Error('时间偏移与 timeZone 不一致，请确认时区');
          }
          candidate = {id: updateId || randomUUID(), title: input.title.trim(), start: input.start, end: input.end, timeZone: input.timeZone};
          const conflicts = inRange(doc.events.filter(e => e.id !== updateId), candidate.start, candidate.end);
          if (conflicts.length) {
            const confirmationId = randomUUID();
            doc.pending[confirmationId] = {event: candidate, conflicts, authorized: false, ...(updateId ? {updateId, original: {...original}} : {})};
            const result = {status: 'needs_confirmation', confirmationId, event: candidate, conflicts};
            doc.operations[api.callId] = result;
            return output(result);
          }
        }
        if (!updateId && doc.events.length >= 200) throw new Error('MVP 日程上限为 200 条');
        if (updateId) doc.events.splice(doc.events.findIndex(e => e.id === updateId), 1, candidate);
        else doc.events.push(candidate);
        const result = {status: updateId ? 'updated' : 'created', event: candidate};
        doc.operations[api.callId] = result;
        if (pending) pending.result = result;
        await tx.appendEntry(updateId ? Updated : Created, api.conversationId, {data: candidate});
        return output(result);
      }, context);
    }});
  return [defineTool({name: 'list_calendar_events',
    description: 'Read saved calendar events overlapping an explicit start/end range. Ask for missing dates; never invent results.',
    parameters: Type.Object({start: Type.String(), end: Type.String()}), replay: 'safe',
    execute: async ({start, end}, api, context) => {
      event('tool');
      return api.commit(async tx => output({events: inRange((await tx.doc(Calendar, api.conversationId)).events, start, end)}), context);
    }}), write(), write(true), defineTool({name: 'delete_calendar_event',
    description: 'Delete ONE saved event by eventId obtained from list_calendar_events. Resolve ambiguous matches with the user first. Only status deleted means success.',
    parameters: Type.Object({eventId: Type.String()}), replay: 'safe',
    execute: async ({eventId}, api, context) => {
      event('tool');
      return api.commit(async tx => {
        const doc = await tx.doc(Calendar, api.conversationId);
        if (Object.hasOwn(doc.operations, api.callId)) return output(doc.operations[api.callId]);
        const index = doc.events.findIndex(e => e.id === eventId);
        const result = index < 0 ? {status: 'not_found', eventId} : {status: 'deleted', event: doc.events.splice(index, 1)[0]};
        doc.operations[api.callId] = result;
        if (index >= 0) await tx.appendEntry(Deleted, api.conversationId, {data: result.event});
        return output(result);
      }, context);
    }}), defineTool({name: 'get_current_time',
    description: 'Return the current instant in the project timezone (fixed UTC+8) together with today, tomorrow, dayAfterTomorrow and the Chinese weekday. Call this before resolving any relative date or time.',
    parameters: Type.Object({}), replay: 'safe',
    execute: async () => {
      event('tool');
      const now = nowInDefaultZone();
      const today = now.slice(0, 10);
      const weekday = '日一二三四五六'[new Date(today + 'T00:00:00Z').getUTCDay()];
      return output({now, timeZone: DEFAULT_TIME_ZONE, today, tomorrow: addDays(today, 1),
        dayAfterTomorrow: addDays(today, 2), weekday: '周' + weekday});
    }})];
}
