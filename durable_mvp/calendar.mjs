import {randomUUID} from 'node:crypto';
import {Type} from '@earendil-works/pi-ai';
import {defineDoc, defineEntry, defineTool} from '@earendil-works/pi-durable';

export const Calendar = defineDoc({kind: 'mvp.calendar', version: 1, scope: 'conversation',
  history: 'rewindable', fork: 'asOf', initial: () => ({events: [], operations: {}, pending: {}}), checkpointWhen: () => false});
const Updated = defineEntry('mvp.calendar-updated');
const Deleted = defineEntry('mvp.calendar-deleted');
const Created = defineEntry('mvp.calendar-created');
const output = value => ({output: [{type: 'text', text: JSON.stringify(value)}]});
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
export function calendarTools(event) {
  const write = (updating = false) => defineTool({name: updating ? 'update_calendar_event' : 'create_calendar_event',
    description: 'Create or update ONE event with title, start, end (explicit offsets) and optional IANA timeZone (defaults to Etc/GMT+8, fixed UTC-8). Ask user for missing title/date/start/end. On needs_confirmation ask user to use the page confirmation button. Never claim success unless status is created or updated. confirmationId retries a pending event; only server-recorded user confirmation authorizes overlap. For update pass eventId from list_calendar_events; omitted fields retain saved values. No recurrence/reminders.',
    parameters: Type.Object({eventId: Type.Optional(Type.String()), title: Type.Optional(Type.String({maxLength: 200})), start: Type.Optional(Type.String()),
      end: Type.Optional(Type.String()), timeZone: Type.Optional(Type.String()), confirmationId: Type.Optional(Type.String())}), replay: 'safe',
    execute: async (input, api, context) => {
      event('tool');
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
          input = {...original, ...input, timeZone: input.timeZone || original?.timeZone || 'Etc/GMT+8'};
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
    }})];
}
