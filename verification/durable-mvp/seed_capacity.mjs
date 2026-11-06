// Populate a real Durable document in the test-owned temporary store; no implementation mocks.
import path from 'node:path';
import {BACKGROUND_CONTEXT as ctx} from '@earendil-works/chord/context';
import {Harness, createRegistry} from '@earendil-works/pi-durable';
import {createModels} from '@earendil-works/pi-ai/models';
import {openNodeSqliteStorage} from '@earendil-works/pi-durable/storage/sqlite/node';
import {Calendar} from './calendar.mjs';
const harness = await Harness.open(await openNodeSqliteStorage(path.join(process.argv[2], 'session.sqlite')),
  {models: createModels(), registry: createRegistry()}, ctx);
try {
  const root = await harness.root(ctx);
  await root.commit(async tx => {
    const doc = await tx.doc(Calendar, root.id);
    doc.events = Array.from({length: 200}, (_, i) => ({id: `fixture-${i}`, title: '容量测试',
      start: '2026-11-06T14:00:00+08:00', end: '2026-11-06T15:00:00+08:00', timeZone: 'Etc/GMT-8'}));
  }, ctx);
} finally { await harness.close(ctx); }
