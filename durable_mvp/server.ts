import fs from 'node:fs';
import path from 'node:path';
import net from 'node:net';
import {DatabaseSync} from 'node:sqlite';
import {syncBuiltinESMExports} from 'node:module';
import {parseArgs} from 'node:util';
import {BACKGROUND_CONTEXT as ctx} from '@earendil-works/chord/context';
import {openNodeSqliteStorage} from '@earendil-works/pi-durable/storage/sqlite/node';
import {Harness, createRegistry} from '@earendil-works/pi-durable';
import {createAgentExtension, instructions} from './agent.ts';
import {configureModels} from './models.ts';
import {setNowProvider} from './calendar-time.ts';
import {createHttpServer} from './http.ts';

const {values: options} = parseArgs({options: {
  mode: {type: 'string', default: 'offline'}, data: {type: 'string'},
  port: {type: 'string', default: '8765'}, 'now-file': {type: 'string'},
}});
if (options.mode !== 'offline' && options.mode !== 'deepseek') throw new Error('mode must be offline or deepseek');
// Test-only: pin "now" from a file (or literal ISO string). Production never passes this flag.
if (options['now-file'] !== undefined) {
  const source = fs.existsSync(options['now-file']) ? fs.readFileSync(options['now-file'], 'utf8').trim() : options['now-file'];
  const fixed = Date.parse(source);
  if (!/(?:Z|[+-]\d{2}:\d{2})$/.test(source) || !Number.isFinite(fixed))
    throw new Error('Invalid --now-file: expected an ISO timestamp with Z or an explicit offset');
  setNowProvider(() => fixed);
}
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
// A separate lock database holds an EXCLUSIVE transaction for the lifetime of this
// process. The write, not BEGIN alone, is what takes the lock; the transaction stays
// open, so the owner row is never committed. The OS releases the lock on exit or kill,
// so there is no stale pid file to reclaim.
const lockDb = new DatabaseSync(path.join(folder, 'host.lock.sqlite'));
try {
  lockDb.exec('PRAGMA locking_mode=EXCLUSIVE');
  lockDb.exec('PRAGMA busy_timeout=0');
  lockDb.exec('BEGIN EXCLUSIVE');
  lockDb.exec('CREATE TABLE IF NOT EXISTS host (pid INTEGER NOT NULL, started_at TEXT NOT NULL)');
  lockDb.exec('DELETE FROM host');
  lockDb.prepare('INSERT INTO host (pid, started_at) VALUES (?, ?)').run(process.pid, new Date().toISOString());
} catch (error) {
  // Only SQLITE_BUSY/SQLITE_LOCKED means another host holds this store; other failures
  // (corruption, permissions, disk) must keep their own message.
  if (error instanceof Error && 'errcode' in error && (error.errcode === 5 || error.errcode === 6)) throw new Error('Store is already in use: ' + error.message);
  throw error;
}

const counts = {model: 0, tool: 0};
const event = (kind: keyof typeof counts) => { counts[kind]++; console.log(JSON.stringify({event: kind, count: counts[kind]})); };
const {models, model} = configureModels(offline, event);
const extension = createAgentExtension(event);
const registry = createRegistry();
registry.install(extension);
const storage = await openNodeSqliteStorage(path.join(folder, 'session.sqlite'));
const harness = await Harness.open(storage, {models, registry,
  settings: {retry: {maxRetries: 0}, toolExecution: 'sequential'},
  onReport: () => console.error('Durable reported a runtime error; inspect state before continuing.')}, ctx);
const root = await harness.root(ctx, {agent: {model, extensions: [extension], thinkingLevel: 'off', instructions}});
const existing = await root.agent(ctx);
if (existing.model?.provider !== model.provider || existing.model?.modelId !== model.modelId)
  throw new Error('Stored model differs; use a separate --data directory');
// root() only applies defaults when creating a conversation. Refresh the app-owned
// instructions before resuming existing sessions, without replacing their data.
if (existing.instructions !== instructions) await root.configure({instructions}, ctx);
const server = createHttpServer(harness, root, {mode: options.mode, model, counts});
// Resuming unfinished model/tool tasks can repeat paid calls in real mode.
harness.resume();
server.listen(port, '127.0.0.1', () => {
  const address = server.address();
  if (address && typeof address === 'object') console.log('DURABLE_MVP http://127.0.0.1:' + address.port);
});
async function stop() {
  server.close();
  await harness.close(ctx);
  // Keep the lock connection reachable for the entire lifetime of the host.
  lockDb.close();
  process.exit(0);
}
process.once('SIGINT', stop);
process.once('SIGTERM', stop);
