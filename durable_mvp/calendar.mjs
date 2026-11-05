import {randomUUID} from 'node:crypto';
import {Type} from '@earendil-works/pi-ai';
import {defineDoc, defineEntry, defineTool} from '@earendil-works/pi-durable';

export const Calendar = defineDoc({kind: 'mvp.calendar', version: 1, scope: 'conversation',
  history: 'rewindable', fork: 'asOf', initial: () => ({events: [], operations: {}, pending: {}}), checkpointWhen: () => false});
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
  return [defineTool({name: 'list_calendar_events',
    description: 'Read saved calendar events overlapping an explicit start/end range. Ask for missing dates; never invent results.',
    parameters: Type.Object({start: Type.String(), end: Type.String()}), replay: 'safe',
    execute: async ({start, end}, api, context) => {
      event('tool');
      return api.commit(async tx => output({events: inRange((await tx.doc(Calendar, api.conversationId)).events, start, end)}), context);
    }}), defineTool({name: 'create_calendar_event',
    description: 'Create ONE event with title, start, end and timeZone (IANA). Ask user for missing information. On needs_confirmation ask user to use the page confirmation button. Never claim success unless status is created. confirmationId retries a pending event; only server-recorded user confirmation authorizes overlap. No editing/deleting/recurrence/reminders.',
    parameters: Type.Object({title: Type.Optional(Type.String({maxLength: 200})), start: Type.Optional(Type.String()),
      end: Type.Optional(Type.String()), timeZone: Type.Optional(Type.String()), confirmationId: Type.Optional(Type.String())}), replay: 'safe',
    execute: async (input, api, context) => {
      event('tool');
      return api.commit(async tx => {
        const doc = await tx.doc(Calendar, api.conversationId);
        if (Object.hasOwn(doc.operations, api.callId)) return output(doc.operations[api.callId]);
        let candidate, pending;
        if (input.confirmationId) {
          pending = doc.pending[input.confirmationId];
          if (!pending) throw new Error('待确认日程不存在');
          if (pending.result) return output(pending.result);
          if (!pending.authorized) return output({status: 'needs_confirmation', confirmationId: input.confirmationId, event: pending.event});
          candidate = pending.event;
        } else {
          const missing = ['title', 'start', 'end', 'timeZone'].filter(key => !input[key]?.trim());
          if (missing.length) return output({status: 'needs_information', missing, message: '请向用户追问缺少的信息'});
          range(input.start, input.end);
          const formatter = new Intl.DateTimeFormat('sv-SE', {timeZone: input.timeZone, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23'});
          for (const value of [input.start, input.end]) {
            if (formatter.format(new Date(value)).replace(' ', 'T') !== value.slice(0, 19)) throw new Error('时间偏移与 timeZone 不一致，请确认时区');
          }
          candidate = {id: randomUUID(), title: input.title.trim(), start: input.start, end: input.end, timeZone: input.timeZone};
          const conflicts = inRange(doc.events, candidate.start, candidate.end);
          if (conflicts.length) {
            const confirmationId = randomUUID();
            doc.pending[confirmationId] = {event: candidate, authorized: false};
            const result = {status: 'needs_confirmation', confirmationId, event: candidate, conflicts};
            doc.operations[api.callId] = result;
            return output(result);
          }
        }
        if (doc.events.length >= 200) throw new Error('MVP 日程上限为 200 条');
        doc.events.push(candidate);
        const result = {status: 'created', event: candidate};
        doc.operations[api.callId] = result;
        if (pending) pending.result = result;
        await tx.appendEntry(Created, api.conversationId, {data: candidate});
        return output(result);
      }, context);
    }})];
}
