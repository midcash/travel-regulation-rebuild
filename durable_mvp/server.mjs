// A single local host. Pi Durable owns all conversation, document and task state.
import fs from 'node:fs';
import path from 'node:path';
import http from 'node:http';
import net from 'node:net';
import {syncBuiltinESMExports} from 'node:module';
import {fileURLToPath} from 'node:url';
import {parseArgs} from 'node:util';
import {setTimeout as delay} from 'node:timers/promises';
import {BACKGROUND_CONTEXT as ctx} from '@earendil-works/chord/context';
import {Type} from '@earendil-works/pi-ai';
import {createModels} from '@earendil-works/pi-ai/models';
import {deepseekProvider} from '@earendil-works/pi-ai/providers/deepseek';
import {fauxProvider, fauxAssistantMessage, fauxToolCall} from '@earendil-works/pi-ai/providers/faux';
import {Harness, createRegistry, defineDoc, defineEntry, defineExtension, defineTool, defineTask, section}
  from '@earendil-works/pi-durable';
import {openNodeSqliteStorage} from '@earendil-works/pi-durable/storage/sqlite/node';

const {values: options} = parseArgs({options: {
  mode: {type: 'string', default: 'offline'}, data: {type: 'string'},
  port: {type: 'string', default: '8765'},
}});
if (!['offline', 'deepseek'].includes(options.mode)) throw new Error('mode must be offline or deepseek');
const port = Number(options.port);
if (!Number.isInteger(port) || port < 0 || port > 65535) throw new Error('Invalid port');
const offline = options.mode === 'offline';
if (!offline && !process.env.DEEPSEEK_API_KEY) throw new Error('Missing DEEPSEEK_API_KEY');
if (offline) {
  const deny = () => { throw new Error('Offline mode blocks outbound network'); };
  globalThis.fetch = deny;
  net.Socket.prototype.connect = deny;
  syncBuiltinESMExports();
}
const folder = path.resolve(options.data ?? `.durable-mvp/${options.mode}`);
fs.mkdirSync(folder, {recursive: true});
// OS-level SQLite locking alone does not exclude two independent harness schedulers.
const lockPath = path.join(folder, 'host.lock');
try {
  const owner = Number(fs.readFileSync(lockPath, 'utf8'));
  if (!Number.isInteger(owner) || owner <= 0) throw new Error('Invalid host lock; inspect it manually');
  try { process.kill(owner, 0); throw new Error('Store is already in use'); }
  catch (error) { if (error.code !== 'ESRCH') throw error; }
  fs.unlinkSync(lockPath);
} catch (error) { if (error.code !== 'ENOENT') throw error; }
fs.writeFileSync(lockPath, String(process.pid), {flag: 'wx'});
process.on('exit', () => { try { fs.unlinkSync(lockPath); } catch {} });

const Notebook = defineDoc({kind: 'mvp.notebook', version: 1, scope: 'conversation',
  history: 'rewindable', fork: 'asOf', initial: () => ({text: '', callId: ''}), checkpointWhen: () => false});
const Changed = defineEntry('mvp.note-changed');
const Timers = defineDoc({kind: 'mvp.timers', version: 1, scope: 'conversation',
  history: 'rewindable', fork: 'asOf', initial: () => ({items: []}), checkpointWhen: () => false});
const Fired = defineEntry('mvp.timer-fired');
const Requests = defineDoc({kind: 'mvp.requests', version: 1, scope: 'conversation',
  history: 'rewindable', fork: 'asOf', initial: () => ({items: {}}), checkpointWhen: () => false});
const Timer = defineTask({name: 'mvp.timer', version: 1, initial: () => ({phase: 'arm'}), phases: {
  arm: async (task, runtime, context) => runtime.sleepUntil(task.input.until, {phase: 'fire'}, context),
  fire: async (task, runtime, context) => runtime.commit(async tx => {
    await tx.appendEntry(Fired, task.input.conversationId, {data: {text: '计时完成', until: task.input.until}});
    return {status: 'terminal', outcome: {status: 'completed', result: '计时完成'}};
  }, context),
}, abort: async (_task, runtime, context) => runtime.commit(() => ({status: 'terminal', outcome: {status: 'aborted'}}), context)});
const counts = {model: 0, tool: 0};
const event = kind => { counts[kind]++; console.log(JSON.stringify({event: kind, count: counts[kind]})); };
const tool = defineTool({name: 'save_note', description: 'Replace the persistent notebook with the requested text.',
  parameters: Type.Object({text: Type.String({maxLength: 4000})}), replay: 'safe',
  execute: async ({text}, api, context) => {
    event('tool');
    await api.commit(async tx => {
      const note = await tx.doc(Notebook, api.conversationId);
      if (note.callId === api.callId) return;
      Object.assign(note, {text, callId: api.callId});
      await tx.appendEntry(Changed, api.conversationId, {data: {text}});
    }, context);
    return {output: [{type: 'text', text: '笔记已保存：' + text}]};
  }});
const slow = defineTool({name: 'slow_echo', description: 'Wait ten seconds then echo text. Safe to repeat after interruption.',
  parameters: Type.Object({text: Type.String({maxLength: 4000})}), replay: 'safe',
  execute: async ({text}, _api, context) => {
    event('tool');
    await delay(10000, undefined, {signal: context.abortSignal});
    return {output: [{type: 'text', text}]};
  }});
const fail = defineTool({name: 'fail_demo', description: 'Deliberately fail to demonstrate error handling.',
  parameters: Type.Object({}), replay: 'safe', execute: async () => { event('tool'); throw new Error('演示工具失败'); }});
const extension = defineExtension({name: 'mvp', tasks: [Timer], tools: [tool, slow, fail],
  sections: [section('notebook', async (input, context) => {
    const note = await input.read.snapshot(Notebook, input.conversationId, context);
    return 'Current notebook: ' + JSON.stringify(note?.text ?? '');
  })]});
const models = createModels();
let model;
if (offline) {
  const faux = fauxProvider();
  const respond = request => {
    const index = request.messages.findLastIndex(m => m.role === 'user');
    const text = request.messages[index]?.content ?? '';
    const result = request.messages.slice(index + 1).findLast(m => m.role === 'toolResult');
    if (result) return fauxAssistantMessage((result.isError ? '工具失败：' : '工具结果：') +
      result.content.filter(b => b.type === 'text').map(b => b.text).join('\n'));
    const [command, ...rest] = String(text).split(' ');
    const name = {'/note': 'save_note', '/slow': 'slow_echo', '/fail': 'fail_demo'}[command];
    if (name) return fauxAssistantMessage(fauxToolCall(name, name === 'fail_demo' ? {} : {text: rest.join(' ')}), {stopReason: 'toolUse'});
    return fauxAssistantMessage('离线回显（非真实模型）：' + text);
  };
  // Replenish scripted responses rather than imposing a finite conversation length.
  const base = faux.provider;
  models.setProvider({...base, streamSimple: (...args) => { faux.appendResponses([respond]); return base.streamSimple(...args); }});
  model = {provider: 'faux', modelId: 'faux-1'};
} else {
  const provider = deepseekProvider();
  const modelId = process.env.DEEPSEEK_MODEL || 'deepseek-flash';
  const known = provider.getModels().find(m => m.id === modelId);
  if (!known) throw new Error('DEEPSEEK_MODEL is not in the locked pi-ai catalog. Supported: ' + provider.getModels().map(m => m.id).join(', '));
  // Model transport, authentication and streaming remain entirely in pi-ai.
  const configured = {...known, baseUrl: process.env.DEEPSEEK_BASE_URL || known.baseUrl};
  models.setProvider({...provider, getModels: () => [configured]});
  model = {provider: 'deepseek', modelId};
}
const stream = models.streamSimple.bind(models);
models.streamSimple = (...args) => { event('model'); return stream(...args); };
const registry = createRegistry();
registry.install(extension);
const storage = await openNodeSqliteStorage(path.join(folder, 'session.sqlite'));
const harness = await Harness.open(storage, {models, registry,
  settings: {retry: {maxRetries: 0}, toolExecution: 'sequential'},
  onReport: () => console.error('Durable reported a runtime error; inspect state before continuing.')}, ctx);
const root = await harness.root(ctx, {agent: {model, extensions: [extension], thinkingLevel: 'off',
  instructions: '你是 Pi Durable 能力验证助手。用中文回答。可用工具用于保存笔记、延迟回显和演示失败。仅声称已成功执行的结果。不提供差旅供应商查询。'}});
const existing = await root.agent(ctx);
if (existing.model?.provider !== model.provider || existing.model?.modelId !== model.modelId)
  throw new Error('Stored model differs; use a separate --data directory');
let fatal = false;
// The framework resumes unfinished model/tool tasks. This can repeat paid calls in real mode.
harness.resume();
harness.closed?.then(() => { fatal = true; });
const here = path.dirname(fileURLToPath(import.meta.url));
async function state() {
  const entries = (await root.context(ctx)).entries;
  const messages = entries.flatMap(entry => (entry.model ?? []).flatMap(message => {
    if (!['user', 'assistant', 'toolResult'].includes(message.role)) return [];
    const text = typeof message.content === 'string' ? message.content : message.content
      .filter(b => b.type === 'text').map(b => b.text).join('\n');
    const calls = Array.isArray(message.content) ? message.content.filter(b => b.type === 'toolCall').map(b => b.name) : [];
    return [{id: entry.id, role: message.role, text, calls, error: message.isError || message.stopReason === 'error'}];
  }));
  const inspection = await harness.inspect(ctx);
  const timerDoc = await harness.snapshot(Timers, root.id, ctx);
  const timers = await Promise.all((timerDoc?.items ?? []).map(async item => ({...item, state: (await harness.getTask(item.id, ctx))?.state})));
  return {conversationId: root.id, mode: options.mode, model, messages,
    note: (await harness.snapshot(Notebook, root.id, ctx))?.text ?? '', timers,
    busy: inspection.tasks.some(t => !t.record.background) || inspection.submissions.length > 0, counts, fatal};
}
function reply(res, status, value) {
  res.writeHead(status, {'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store'});
  res.end(JSON.stringify(value));
}
const server = http.createServer(async (req, res) => {
  const origin = `http://127.0.0.1:${server.address().port}`;
  if (req.headers.host !== origin.slice(7) || (req.headers.origin && req.headers.origin !== origin)) return reply(res, 403, {error: 'Only same-origin loopback requests are allowed'});
  try {
    if (req.method === 'GET' && req.url === '/') {
      res.writeHead(200, {'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store',
        'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'"});
      return res.end(fs.readFileSync(path.join(here, 'index.html')));
    }
    if (req.method === 'GET' && ['/app.js', '/style.css'].includes(req.url)) {
      res.writeHead(200, {'Content-Type': req.url.endsWith('.js') ? 'text/javascript' : 'text/css'});
      return res.end(fs.readFileSync(path.join(here, req.url.slice(1))));
    }
    if (req.method === 'GET' && req.url === '/state') return reply(res, 200, await state());
    if (req.method !== 'POST') return reply(res, 404, {error: 'Not found'});
    if (req.headers['content-type'] !== 'application/json') return reply(res, 415, {error: 'Expected application/json'});
    const chunks = [];
    let bytes = 0;
    for await (const chunk of req) {
      bytes += chunk.length;
      if (bytes > 16000) return reply(res, 413, {error: 'Request too large'});
      chunks.push(chunk);
    }
    const input = JSON.parse(Buffer.concat(chunks).toString('utf8'));
    if (req.url === '/prompt') {
      if (typeof input.text !== 'string' || !input.text.trim() || input.text.length > 4000 ||
        typeof input.requestId !== 'string' || !/^[a-zA-Z0-9][a-zA-Z0-9-]{0,99}$/.test(input.requestId))
        return reply(res, 400, {error: 'Invalid text or requestId'});
      const conflict = await root.commit(async tx => {
        const requests = await tx.doc(Requests, root.id);
        if (Object.hasOwn(requests.items, input.requestId)) return requests.items[input.requestId] !== input.text;
        if (Object.keys(requests.items).length >= 200) throw new Error('MVP conversation limit reached (200 requests)');
        requests.items[input.requestId] = input.text;
        return false;
      }, ctx);
      if (conflict) return reply(res, 409, {error: 'requestId already belongs to different text'});
      const submission = await root.submit({type: 'input', content: input.text, requestId: input.requestId, whenBusy: 'reject'}, ctx);
      return reply(res, 202, {submissionId: submission.id});
    }
    if (req.url === '/abort') { await root.abort(ctx); return reply(res, 200, {aborted: true}); }
    if (req.url === '/timer') {
      const id = await root.commit(async tx => {
        const timers = await tx.doc(Timers, root.id);
        if (timers.items.length >= 20) throw new Error('MVP timer limit reached (20)');
        const until = Date.now() + 10000;
        const id = await tx.createTask(Timer, {until, conversationId: root.id}, {ownership: {kind: 'conversation'}, background: true});
        timers.items.push({id, until});
        return id;
      }, ctx);
      harness.resume();
      return reply(res, 202, {taskId: id});
    }
    return reply(res, 404, {error: 'Not found'});
  } catch (error) {
    reply(res, error.name === 'ConversationBusy' ? 409 : 400, {error: String(error.message)});
  }
});
server.listen(port, '127.0.0.1', () => console.log('DURABLE_MVP ' + `http://127.0.0.1:${server.address().port}`));
async function stop() { server.close(); await harness.close(ctx); process.exit(0); }
process.on('SIGINT', stop);
process.on('SIGTERM', stop);
