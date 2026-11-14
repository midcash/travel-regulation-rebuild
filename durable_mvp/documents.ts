import {defineDoc, defineEntry, type TaskId} from '@earendil-works/pi-durable';

export const Notebook = defineDoc({kind: 'mvp.notebook', version: 1, scope: 'conversation',
  history: 'rewindable', fork: 'asOf', initial: () => ({text: '', callId: ''}), checkpointWhen: () => false});
export const Changed = defineEntry<{text: string}>('mvp.note-changed');
export const Timers = defineDoc<{items: {id: TaskId<string>; until: number}[]}>({kind: 'mvp.timers', version: 1, scope: 'conversation',
  history: 'rewindable', fork: 'asOf', initial: () => ({items: []}), checkpointWhen: () => false});
export const Fired = defineEntry<{text: string; until: number}>('mvp.timer-fired');
export const Requests = defineDoc<{items: Record<string, string>}>({kind: 'mvp.requests', version: 1, scope: 'conversation',
  history: 'rewindable', fork: 'asOf', initial: () => ({items: {}}), checkpointWhen: () => false});
