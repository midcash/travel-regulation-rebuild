import fs from 'node:fs';
import path from 'node:path';
import http, {type OutgoingHttpHeaders, type ServerResponse} from 'node:http';
import {BACKGROUND_CONTEXT as ctx} from '@earendil-works/chord/context';
import {ConversationBusy, type Harness, type Conversation, type ModelRef} from '@earendil-works/pi-durable';
import {Timer} from './agent.ts';
import {Calendar, inRange} from './calendar.ts';
import {Notebook, Requests, Timers} from './documents.ts';

const assets = new Map([
  ['/style.css', 'style.css'],
  ['/fullcalendar.js', 'node_modules/fullcalendar/index.global.min.js'],
  ['/calendar-zh-cn.js', 'node_modules/@fullcalendar/core/locales/zh-cn.global.min.js'],
  ['/luxon.js', 'node_modules/luxon/build/global/luxon.min.js'],
  ['/calendar-zone.js', 'node_modules/@fullcalendar/luxon3/index.global.min.js'],
]);

interface HostMetadata {
  mode: 'offline' | 'deepseek';
  model: ModelRef;
  counts: {model: number; tool: number};
}

function reply(res: ServerResponse, status: number, value: unknown, headers: OutgoingHttpHeaders = {}) {
  res.writeHead(status, {'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store', ...headers});
  res.end(JSON.stringify(value));
}
export function createHttpServer(harness: Harness, root: Conversation, metadata: HostMetadata) {
  let fatal = false;
  void harness.closed?.then(() => { fatal = true; });

  async function state() {
    const entries = (await root.context(ctx)).entries;
    const messages = entries.flatMap(entry => (entry.model ?? []).flatMap(message => {
      if (message.role !== 'user' && message.role !== 'assistant' && message.role !== 'toolResult') return [];
      const text = typeof message.content === 'string' ? message.content : message.content
        .filter(b => b.type === 'text').map(b => b.text).join('\n');
      const calls = Array.isArray(message.content) ? message.content.flatMap(b => b.type === 'toolCall' ? [b.name] : []) : [];
      return [{id: entry.id, role: message.role, text, calls, error: (message.role === 'toolResult' && message.isError) || (message.role === 'assistant' && message.stopReason === 'error')}];
    }));
    const inspection = await harness.inspect(ctx);
    const timerDoc = await harness.snapshot(Timers, root.id, ctx);
    const timers = await Promise.all((timerDoc?.items ?? []).map(async item => ({...item, state: (await harness.getTask(item.id, ctx))?.state})));
    return {conversationId: root.id, ...metadata, messages,
      note: (await harness.snapshot(Notebook, root.id, ctx))?.text ?? '', timers,
      calendar: (await harness.snapshot(Calendar, root.id, ctx)) ?? {events: [], pending: {}},
      busy: inspection.tasks.some(t => !t.record.background) || inspection.submissions.length > 0, fatal};
  }
  const server = http.createServer(async (req, res) => {
    const address = server.address();
    if (!address || typeof address === 'string') return reply(res, 503, {error: 'Host is not listening'});
    const origin = `http://127.0.0.1:${address.port}`;
    if (req.headers.host !== origin.slice(7) || (req.headers.origin && req.headers.origin !== origin))
      return reply(res, 403, {error: 'Only same-origin loopback requests are allowed'});
    const url = req.url ?? '';
    try {
      if (req.method === 'GET' && url === '/') {
        const html = fs.readFileSync(path.join(import.meta.dirname, 'index.html'));
        res.writeHead(200, {'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store',
          'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; font-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"});
        return res.end(html);
      }
      const asset = /^\/web\/[a-z-]+\.js$/.test(url) ? 'dist' + url : assets.get(url);
      if (req.method === 'GET' && asset) {
        const content = fs.readFileSync(path.join(import.meta.dirname, asset));
        res.writeHead(200, {'Content-Type': url.endsWith('.js') ? 'text/javascript' : 'text/css',
          'Cache-Control': 'no-store'});
        return res.end(content);
      }
      if (req.method === 'GET' && url.startsWith('/calendar?')) {
        const query = new URL(url, origin).searchParams;
        return reply(res, 200, inRange((await harness.snapshot(Calendar, root.id, ctx))?.events ?? [], query.get('start'), query.get('end')));
      }
      if (req.method === 'GET' && url === '/state') return reply(res, 200, await state());
      if (req.method !== 'POST') return reply(res, 404, {error: 'Not found'});
      const mediaType = (req.headers['content-type'] ?? '').split(';', 1)[0].trim().toLowerCase();
      // Rejections before the body is read must close the connection; the rest stays unread.
      if (mediaType !== 'application/json') return reply(res, 415, {error: 'Expected application/json'}, {Connection: 'close'});
      const chunks: Buffer[] = [];
      let bytes = 0;
      for await (const chunk of req) {
        bytes += chunk.length;
        if (bytes > 16000) return reply(res, 413, {error: 'Request too large'}, {Connection: 'close'});
        chunks.push(chunk);
      }
      const input: unknown = JSON.parse(Buffer.concat(chunks).toString('utf8'));
      if (url === '/prompt') {
        if (!input || typeof input !== 'object' || !('text' in input) || !('requestId' in input) ||
          typeof input.text !== 'string' || !input.text.trim() || input.text.length > 4000 ||
          typeof input.requestId !== 'string' || !/^[a-zA-Z0-9][a-zA-Z0-9-]{0,99}$/.test(input.requestId))
          return reply(res, 400, {error: 'Invalid text or requestId'});
        const {text, requestId} = input;
        const conflict = await root.commit(async tx => {
          const requests = await tx.doc(Requests, root.id);
          if (Object.hasOwn(requests.items, requestId)) return requests.items[requestId] !== text;
          if (Object.keys(requests.items).length >= 200) throw new Error('MVP conversation limit reached (200 requests)');
          requests.items[requestId] = text;
          const confirmation = /^确认重叠 ([a-f0-9-]{36})$/.exec(text.trim());
          if (confirmation) {
            const calendar = await tx.doc(Calendar, root.id);
            if (calendar.pending[confirmation[1]]) calendar.pending[confirmation[1]].authorized = true;
          }
          return false;
        }, ctx);
        if (conflict) return reply(res, 409, {error: 'requestId already belongs to different text'});
        const submission = await root.submit({type: 'input', content: text, requestId, whenBusy: 'reject'}, ctx);
        return reply(res, 202, {submissionId: submission.id});
      }
      if (url === '/abort') { await root.abort(ctx); return reply(res, 200, {aborted: true}); }
      if (url === '/timer') {
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
      reply(res, error instanceof ConversationBusy ? 409 : 400, {error: error instanceof Error ? error.message : String(error)});
    }
  });
  return server;
}
