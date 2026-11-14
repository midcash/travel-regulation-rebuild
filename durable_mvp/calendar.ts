import {randomUUID} from 'node:crypto';
import {Type} from '@earendil-works/pi-ai';
import {defineDoc, defineEntry, defineTool} from '@earendil-works/pi-durable';
import {addDays, currentTimeMs, DEFAULT_TIME_ZONE, instant, nowInDefaultZone, range, resolveWhen, zonedClock} from './calendar-time.ts';

export type CalendarEvent = {
  id: string;
  title: string;
  start: string;
  end: string;
  timeZone: string;
};

type CalendarResult =
  | {status: 'not_found'; eventId: string}
  | {status: 'stale'; message: string}
  | {status: 'needs_confirmation'; confirmationId: string; event: CalendarEvent; conflicts: CalendarEvent[]; message?: string}
  | {status: 'created' | 'updated' | 'deleted'; event: CalendarEvent};

type PendingEvent = {
  event: CalendarEvent;
  conflicts: CalendarEvent[];
  authorized: boolean;
  updateId?: string;
  original?: CalendarEvent;
  result?: CalendarResult;
};

type CalendarDocument = {
  events: CalendarEvent[];
  operations: Record<string, CalendarResult>;
  pending: Record<string, PendingEvent>;
};

export const Calendar = defineDoc<CalendarDocument>({kind: 'mvp.calendar', version: 1, scope: 'conversation',
  history: 'rewindable', fork: 'asOf', initial: () => ({events: [], operations: {}, pending: {}}), checkpointWhen: () => false});
const Updated = defineEntry<CalendarEvent>('mvp.calendar-updated');
const Deleted = defineEntry<CalendarEvent>('mvp.calendar-deleted');
const Created = defineEntry<CalendarEvent>('mvp.calendar-created');
const output = (value: unknown) => ({output: [{type: 'text' as const, text: JSON.stringify(value)}]});
export function inRange<T extends Pick<CalendarEvent, 'start' | 'end'>>(events: readonly T[], start: unknown, end: unknown) {
  const [a, b] = range(start, end);
  return events.filter(e => Date.parse(e.start) < b && Date.parse(e.end) > a);
}

export function calendarTools(event: (kind: 'tool') => void) {
  const write = (updating = false) => defineTool({name: updating ? 'update_calendar_event' : 'create_calendar_event',
    description: (updating ? 'Update ONE saved event by eventId from list_calendar_events. When moving it with when, preserve its duration unless a new end or duration is specified. Omitted fields retain saved values.' : 'Create ONE NEW event with a title and start/end timing. Use explicit offsets for start and end, or use when for the start and provide an end or durationMinutes inside when. Ask for missing creation fields.') + ` Call get_current_time before interpreting relative dates and pass a structured when for server-side resolution. Optional IANA timeZone defaults to the saved zone on update or ${DEFAULT_TIME_ZONE} (fixed UTC+8) on creation. On needs_confirmation use the page confirmation button. Only status created or updated means success. confirmationId retries a pending operation; only server-recorded user confirmation authorizes overlap. No recurrence/reminders.`,
    parameters: Type.Object({...(updating ? {eventId: Type.Optional(Type.String())} : {}), title: Type.Optional(Type.String({maxLength: 200})), start: Type.Optional(Type.String()),
      end: Type.Optional(Type.String()), timeZone: Type.Optional(Type.String()),
      when: Type.Optional(Type.Object({day: Type.Optional(Type.String()), date: Type.Optional(Type.String()),
        absolute: Type.Optional(Type.String()), time: Type.Optional(Type.String()),
        offsetMinutes: Type.Optional(Type.Number()), durationMinutes: Type.Optional(Type.Number())})),
      confirmationId: Type.Optional(Type.String())}), replay: 'safe',
    execute: async (input, api, context) => {
      event('tool');
      if (!updating && Object.hasOwn(input, 'eventId')) throw new Error('创建工具不接受 eventId；修改请使用 update_calendar_event');
      return api.commit(async tx => {
        const doc = await tx.doc(Calendar, api.conversationId);
        if (Object.hasOwn(doc.operations, api.callId)) return output(doc.operations[api.callId]);
        if (input.confirmationId && doc.pending[input.confirmationId]?.result) return output(doc.pending[input.confirmationId].result);
        if (!updating && !input.confirmationId && doc.events.length >= 200) throw new Error('MVP 日程上限为 200 条');
        let candidate: CalendarEvent;
        let pending: PendingEvent | undefined;
        let original: CalendarEvent | undefined;
        const updateId = input.confirmationId
          ? doc.pending[input.confirmationId]?.updateId
          : updating && typeof input.eventId === 'string' ? input.eventId : undefined;
        if (updating && !input.confirmationId && !updateId) return output({status: 'needs_information', missing: ['eventId']});
        if (updateId) {
          original = doc.events.find(e => e.id === updateId);
          if (!original) {
            const result: CalendarResult = {status: 'not_found', eventId: updateId};
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
            const result: CalendarResult = {status: 'stale', message: '原日程已改变，请重新查询并修改'};
            pending.result = result;
            doc.operations[api.callId] = result;
            return output(result);
          }
          candidate = pending.event;
          const conflicts = inRange(doc.events.filter(e => e.id !== updateId), candidate.start, candidate.end);
          const previousConflicts = pending.conflicts ?? [];
          if (conflicts.some(e => !previousConflicts.some(old => old.id === e.id && old.start === e.start && old.end === e.end))) {
            pending.authorized = false;
            pending.conflicts = conflicts;
            const result: CalendarResult = {status: 'needs_confirmation', confirmationId: input.confirmationId, event: candidate, conflicts, message: '出现新的冲突，请用户重新确认'};
            doc.operations[api.callId] = result;
            return output(result);
          }
        } else {
          const timeZone = input.timeZone || original?.timeZone || DEFAULT_TIME_ZONE;
          if (input.when !== undefined) {
            const resolved = resolveWhen(input.when, currentTimeMs(), timeZone);
            if (!resolved.ok) return output({status: 'needs_information', missing: resolved.missing ?? [],
              reason: resolved.reason, spec: input.when});
            if (input.start !== undefined && Date.parse(input.start) !== Date.parse(resolved.start))
              return output({status: 'needs_information', missing: [], reason: 'start 与 when 冲突', spec: input.when});
            let end = resolved.end ?? input.end;
            if (end === undefined && original) {
              const [oldStart, oldEnd] = range(original.start, original.end);
              end = zonedClock(timeZone).format(instant(resolved.start) + oldEnd - oldStart);
            }
            input = {...input, start: resolved.start, end};
          }
          input = {...original, ...input};
          const {title, start, end} = input;
          if (!title?.trim() || !start?.trim() || !end?.trim()) {
            const missing = (['title', 'start', 'end'] as const).filter(key => !input[key]?.trim());
            return output({status: 'needs_information', missing, message: '请向用户追问缺少的信息'});
          }
          range(start, end);
          const formatter = new Intl.DateTimeFormat('sv-SE', {timeZone, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23'});
          for (const value of [start, end]) {
            if (formatter.format(new Date(value)).replace(' ', 'T') !== value.slice(0, 19)) throw new Error('时间偏移与 timeZone 不一致，请确认时区');
          }
          candidate = {id: updateId || randomUUID(), title: title.trim(), start, end, timeZone};
          const conflicts = inRange(doc.events.filter(e => e.id !== updateId), candidate.start, candidate.end);
          if (conflicts.length) {
            const confirmationId = randomUUID();
            doc.pending[confirmationId] = {event: candidate, conflicts, authorized: false, ...(original ? {updateId, original: {...original}} : {})};
            const result: CalendarResult = {status: 'needs_confirmation', confirmationId, event: candidate, conflicts};
            doc.operations[api.callId] = result;
            return output(result);
          }
        }
        if (!updateId && doc.events.length >= 200) throw new Error('MVP 日程上限为 200 条');
        if (updateId) doc.events.splice(doc.events.findIndex(e => e.id === updateId), 1, candidate);
        else doc.events.push(candidate);
        const result: CalendarResult = {status: updateId ? 'updated' : 'created', event: candidate};
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
        const result: CalendarResult = index < 0 ? {status: 'not_found', eventId} : {status: 'deleted', event: doc.events.splice(index, 1)[0]};
        doc.operations[api.callId] = result;
        if (result.status === 'deleted') await tx.appendEntry(Deleted, api.conversationId, {data: result.event});
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
