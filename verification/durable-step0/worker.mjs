// Isolated issue #44 probe. Session durability belongs entirely to Pi Durable.
import fs from 'node:fs';
import readline from 'node:readline';
import net from 'node:net';
import http from 'node:http';
import https from 'node:https';
import {syncBuiltinESMExports} from 'node:module';

const emit = value => process.stdout.write(JSON.stringify(value) + '\n');
const pending = new Map();
let serial = 0;
function rpc(kind, value = {}) {
  const id = ++serial;
  return new Promise(resolve => {
    pending.set(id, resolve);
    emit({rpc: id, kind, ...value});
  });
}
let networkAttempts = 0;
function denyNetwork() {
  networkAttempts++;
  emit({kind: 'network-blocked'});
  throw new Error('Offline probe denies network');
}
globalThis.fetch = denyNetwork;
net.Socket.prototype.connect = denyNetwork;
http.request = http.get = https.request = https.get = denyNetwork;
syncBuiltinESMExports();

const {BACKGROUND_CONTEXT: context} = await import('@earendil-works/chord/context');
const {Type} = await import('@earendil-works/pi-ai');
const {createModels} = await import('@earendil-works/pi-ai/models');
const {fauxProvider, fauxAssistantMessage, fauxToolCall} = await import('@earendil-works/pi-ai/providers/faux');
const {Harness, createRegistry, defineDoc, defineEntry, defineExtension, defineTool, defineTask, section} =
  await import('@earendil-works/pi-durable');
const {openNodeSqliteStorage, openNodeSqliteDatabase} = await import('@earendil-works/pi-durable/storage/sqlite/node');
const {SqliteStorage} = await import('@earendil-works/pi-durable/storage/sqlite');

const PLAN = {selected: {flight: 'chosen-A'}, recommendations: [{flight: 'suggested-B'}],
  history: [{flight: 'OFFLINE-101', price: null, queriedAt: '2026-10-29T10:00:00+08:00'}]};
const TripPlan = defineDoc({kind: 'probe.trip-plan', version: 1, scope: 'conversation',
  history: 'rewindable', fork: 'asOf', initial: () => ({selected: {}, recommendations: [], history: []}),
  checkpointWhen: () => false});
const PlanChange = defineEntry('probe.plan-change');
let harness, root, invalidResult, commitGate;
const sequences = [], publications = [], views = [];
const Timer = defineTask({name: 'probe.timer', version: 1, initial: () => ({phase: 'arm'}),
  phases: {
    arm: async (task, runtime, ctx) => {
      await runtime.sleepUntil(task.input.until, {phase: 'fire'}, ctx);
    },
    fire: async (_task, runtime, ctx) => {
      await rpc('timer', {at: runtime.now()});
      await runtime.commit(() => ({status: 'terminal', outcome: {status: 'completed', result: 'fired'}}), ctx);
    },
  },
  abort: async (_task, runtime, ctx) => {
    await runtime.commit(() => ({status: 'terminal', outcome: {status: 'aborted'}}), ctx);
  },
});

function makeTool(name, safe) {
  return defineTool({name, description: 'Synthetic Python bridge', parameters: Type.Object({tag: Type.String()}),
    ...(safe ? {replay: 'safe'} : {}),
    execute: async (args, api, ctx) => {
      // Abort signals the invocation; the already-running Python peer still drains.
      const onAbort = () => emit({kind: 'bridge-abort', callId: api.callId});
      ctx.abortSignal.addEventListener('abort', onAbort, {once: true});
      try {
        const result = await rpc('tool', {name, args, callId: api.callId, gate: 'tool:' + args.tag});
        if (name === 'write_plan') {
          await api.commit(async tx => {
            const plan = await tx.doc(TripPlan, api.conversationId);
            Object.assign(plan, structuredClone(PLAN));
            await tx.appendEntry(PlanChange, api.conversationId, {data: PLAN,
              model: [{role: 'user', content: 'TripPlan committed: ' + JSON.stringify(PLAN), timestamp: 0}]});
          }, ctx);
        }
        return {output: [{type: 'text', text: JSON.stringify(result)}]};
      } finally {
        ctx.abortSignal.removeEventListener('abort', onAbort);
      }
    },
  });
}

async function initialize(command) {
  commitGate = command.commitGate;
  if (command.invalid) {
    if (command.invalid === 'corrupt') fs.writeFileSync('session.sqlite', 'not a SQLite file');
    else {
      const {DatabaseSync} = await import('node:sqlite');
      const db = new DatabaseSync('session.sqlite');
      db.exec('CREATE TABLE durable_schema (singleton INTEGER PRIMARY KEY, version INTEGER NOT NULL) STRICT');
      db.exec('INSERT INTO durable_schema VALUES (1, 999999)');
      db.close();
    }
    try {
      const storage = await openNodeSqliteStorage('session.sqlite');
      await storage.close();
      invalidResult = {rejected: false, emptySessionCreated: true};
    } catch (error) {
      invalidResult = {rejected: true, emptySessionCreated: false, error: String(error)};
    }
    return {};
  }
  const faux = fauxProvider();
  const response = async request => {
    const user = request.messages.findLast(m => m.role === 'user' &&
      typeof m.content === 'string' && m.content.startsWith('{"probe":'));
    const input = JSON.parse(user.content);
    await rpc('model', {request, gate: 'model:' + input.tag});
    const answeredTool = request.messages.slice(request.messages.indexOf(user) + 1)
      .some(m => m.role === 'toolResult');
    if (answeredTool || !input.scenario) return fauxAssistantMessage('Synthetic answer.');
    if (input.scenario === 'multi') return fauxAssistantMessage([
      fauxToolCall('search_flights', {tag: 'chain-completed'}),
      fauxToolCall('search_flights', {tag: 'chain-cut'}),
    ], {stopReason: 'toolUse'});
    const name = {safe: 'search_flights', unsafe: 'unsafe_effect', plan: 'write_plan'}[input.scenario];
    return fauxAssistantMessage(fauxToolCall(name, {tag: input.tag}), {stopReason: 'toolUse'});
  };
  faux.setResponses(Array.from({length: 100}, () => response));
  const models = createModels();
  models.setProvider(faux.provider);
  const registry = createRegistry();
  const extension = defineExtension({name: 'probe', tasks: [Timer],
    tools: [makeTool('search_flights', true), makeTool('unsafe_effect', false), makeTool('write_plan', true)],
    sections: [section('trip-plan', async (input, ctx) => {
      const plan = await input.read.snapshot(TripPlan, input.conversationId, ctx);
      return plan ? JSON.stringify(plan) : undefined;
    })],
  });
  registry.install(extension);
  const database = await openNodeSqliteDatabase('session.sqlite');
  const storage = await SqliteStorage.open(database);
  let insidePlanCommit = false;
  const transaction = database.transaction.bind(database);
  database.transaction = change => transaction(async tx => {
    const result = await change(tx);
    if (insidePlanCommit && commitGate === 'inside') await rpc('gate', {gate: 'commit:inside'});
    return result;
  });
  // Instrument only the persistence boundary, without changing the batch or its semantics.
  const commit = storage.commit.bind(storage);
  storage.commit = async (writes, ctx) => {
    const targeted = writes.some(w => w.type === 'entry' && w.value.kind === 'probe.plan-change');
    if (targeted && commitGate === 'before') await rpc('gate', {gate: 'commit:before'});
    if (targeted && command.failCommit) throw new Error('Synthetic storage write failure');
    insidePlanCommit = targeted;
    const seq = await commit(writes, ctx);
    insidePlanCommit = false;
    if (targeted && commitGate === 'after') await rpc('gate', {gate: 'commit:after'});
    return seq;
  };
  harness = await Harness.open(storage, {models, registry, settings: {retry: {maxRetries: 0}, toolExecution: 'sequential'},
    onReport: (error, info) => emit({kind: 'report', error: String(error), info})}, context);
  harness.subscribeCommits(publication => {
    sequences.push(publication.seq);
    publications.push(publication);
  });
  root = await harness.root(context, {agent: {model: {provider: 'faux', modelId: 'faux-1'}, extensions: [extension]}});
  return {};
}

async function handle(c) {
  if (c.rpcResult) {
    const resolve = pending.get(c.rpcResult);
    pending.delete(c.rpcResult);
    resolve?.(c.result);
    return;
  }
  try {
    let result = {};
    switch (c.op) {
      case 'init': result = await initialize(c); break;
      case 'prompt': {
        const submission = await root.submit({type: 'input', requestId: c.requestId,
          content: JSON.stringify({probe: true, tag: c.text, scenario: c.scenario})}, context);
        result = await submission.wait(context);
        break;
      }
      case 'resume': harness.resume(); break;
      case 'idle': await root.waitForIdle(context); break;
      case 'abort': await root.abort(context, {background: c.background ?? false}); break;
      case 'task': result = await harness.getTask(c.taskId, context); break;
      case 'waitTask': result = await harness.waitForTask(c.taskId, context); break;
      case 'timer': {
        result = await root.commit(tx => tx.createTask(Timer, {until: c.until},
          {ownership: {kind: 'conversation'}, background: true}), context);
        harness.resume();
        while ((await harness.getTask(result, context)).state.status !== 'sleeping')
          await new Promise(resolve => setTimeout(resolve, 5));
        break;
      }
      case 'watch': {
        const watch = await root.watch(context);
        views.push(watch.value);
        watch.start(async value => { views.push(value); });
        break;
      }
      case 'views': result = {views}; break;
      case 'snapshot': result = {conversationId: root.id, entries: (await root.context(context)).entries,
        plan: (await harness.snapshot(TripPlan, root.id, context)) ?? null, sequences, publications, views}; break;
      case 'invalidStorage': result = invalidResult; break;
      case 'networkAudit': {
        for (const operation of [() => fetch('https://offline.invalid'),
          () => http.get('http://offline.invalid'), () => net.connect(443, 'offline.invalid')]) {
          try { operation(); } catch { await rpc('network'); }
        }
        result = {attemptsBlocked: networkAttempts};
        break;
      }
      default: throw new Error('Unknown probe command: ' + c.op);
    }
    emit({id: c.id, result});
  } catch (error) {
    emit({id: c.id, error: String(error.stack ?? error)});
  }
}
readline.createInterface({input: process.stdin}).on('line', line => { void handle(JSON.parse(line)); });
