// Reminder business layer for calendar events.
// Timing is owned by the Durable framework task (defineTask + sleepUntil); this
// module only computes *when* to wake and records delivered/expired state in a
// Durable document so delivery is atomic with the conversation.
import {randomUUID} from 'node:crypto';
import {defineDoc, defineEntry, defineTask} from '@earendil-works/pi-durable';

// Default lead time before an event starts. A conversation may override it by
// stating an explicit advance; otherwise this value applies.
export const DEFAULT_ADVANCE_MINUTES = 10;

// A reminder missed on restart is still delivered if the event start is within
// this window in the past; beyond it we only mark it expired (no disturbance).
export const CATCH_UP_WINDOW_MS = 24 * 3600 * 1000;

// Reminder state keyed by calendar event id, committed with the conversation.
// status: 'scheduled' | 'delivered' | 'expired' | 'cancelled'
export const Reminders = defineDoc({kind: 'mvp.reminders', version: 1, scope: 'conversation',
  history: 'rewindable', fork: 'asOf', initial: () => ({items: {}}), checkpointWhen: () => false});
export const ReminderFired = defineEntry('mvp.reminder-fired');

// Resolve the absolute epoch (ms) at which to arm a reminder for `event`, or
// null when no reminder should be armed (no advance / event already started).
// Pure function: no framework, no clock side effects. `now` is the reference
// time so the result is testable with an injected clock.
export function resolveRemindAt(event, advanceMinutes, now) {
  const start = Date.parse(event.start);
  if (!Number.isFinite(start)) return null;
  const advance = Number.isFinite(advanceMinutes) ? advanceMinutes : DEFAULT_ADVANCE_MINUTES;
  if (advance <= 0) return null;
  const remindAt = start - advance * 60 * 1000;
  if (remindAt <= now) return null; // too late to schedule a future reminder
  return remindAt;
}

// Decide what to do with a reminder whose deadline has arrived or was found
// missed on restart. Pure function of (remindAt, eventStart, now):
//   - 'deliver'  : deadline reached, within catch-up window (or in the future)
//   - 'expire'   : deadline long past (event start earlier than window)
export function resolveDelivery(remindAt, eventStart, now) {
  if (now < remindAt) return 'wait';
  if (now - eventStart <= CATCH_UP_WINDOW_MS) return 'deliver';
  return 'expire';
}

// Framework task. arm sleeps until the reminder instant; fire atomically marks
// delivery in the same transaction as other writes, and is idempotent: it only
// marks when this task id is still the current one and not yet delivered, so a
// stale/replayed wake is a no-op.
export function makeReminderTask(RemindersToken) {
  return defineTask({name: 'mvp.reminder', version: 1, initial: () => ({phase: 'arm'}), phases: {
    arm: async (task, runtime, context) =>
      runtime.sleepUntil(task.input.remindAt, {phase: 'fire'}, context),
    fire: async (task, runtime, context) => runtime.commit(async tx => {
      const doc = await tx.doc(RemindersToken, task.input.conversationId);
      const item = doc.items[task.input.eventId];
      // Stale / cancelled / already delivered / superseded task: no-op terminal.
      if (!item || item.taskId !== task.input.taskId || item.status === 'delivered' || item.status === 'cancelled') {
        return {status: 'terminal', outcome: {status: 'completed', result: 'skipped'}};
      }
      const now = runtime.now();
      const eventStart = Date.parse(item.start);
      const decision = resolveDelivery(task.input.remindAt, eventStart, now);
      if (decision === 'expire') {
        // Deadline long past (event start earlier than the catch-up window): do
        // not disturb the user; just mark it expired in the same commit.
        item.status = 'expired';
        item.firedAt = now;
        return {status: 'terminal', outcome: {status: 'completed', result: 'expired'}};
      }
      if (decision === 'wait') {
        // Clock not yet at deadline (e.g. resumed early); re-arm.
        return runtime.sleepUntil(task.input.remindAt, {phase: 'fire'}, context);
      }
      // Atomic mark: delivered state is written in this same commit, so a crash
      // between "fire" and "mark" cannot double-deliver.
      item.status = 'delivered';
      item.firedAt = now;
      await tx.appendEntry(ReminderFired, task.input.conversationId,
        {data: {eventId: task.input.eventId, title: item.title, start: item.start, remindAt: task.input.remindAt}});
      return {status: 'terminal', outcome: {status: 'completed', result: 'delivered'}};
    }, context),
  }, abort: async (_task, runtime, context) =>
    runtime.commit(() => ({status: 'terminal', outcome: {status: 'aborted'}}), context)});
}

// Decide which delivery channels to use given each channel's availability.
// Page channel is the source of truth (it surfaces committed state); the system
// notification is best-effort. Degradation: if the system notification is
// unavailable, we still deliver in-page; if the page channel is somehow
// unavailable but the system one works, use the system one. Delivery is "ok"
// when at least one channel is available. Pure function, unit-testable.
export function resolveDeliveryChannels({page = true, systemNotify = false} = {}) {
  const channels = [];
  if (page) channels.push('page');
  if (systemNotify) channels.push('system');
  return {ok: channels.length > 0, channels};
}

// Plan a reminder record for an event (used inside the calendar transaction).
// Returns the record to store, or null when no reminder applies.
export function planReminder(event, advanceMinutes, now, existing) {
  const remindAt = resolveRemindAt(event, advanceMinutes, now);
  if (remindAt === null) return null;
  const taskId = randomUUID();
  return {eventId: event.id, title: event.title, start: event.start, remindAt,
    advanceMinutes: Number.isFinite(advanceMinutes) ? advanceMinutes : DEFAULT_ADVANCE_MINUTES,
    status: 'scheduled', taskId, createdAt: now};
}
