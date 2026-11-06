// Inspect/change only the test-owned temporary store while its host is stopped.
import path from 'node:path';
import {BACKGROUND_CONTEXT as ctx} from '@earendil-works/chord/context';
import {Harness, createRegistry, defineExtension} from '@earendil-works/pi-durable';
import {createModels} from '@earendil-works/pi-ai/models';
import {openNodeSqliteStorage} from '@earendil-works/pi-durable/storage/sqlite/node';
const registry = createRegistry();
registry.install(defineExtension({name: 'mvp'}));
const harness = await Harness.open(await openNodeSqliteStorage(path.join(process.argv[2], 'session.sqlite')),
  {models: createModels(), registry}, ctx);
try {
  const root = await harness.root(ctx);
  if (process.argv[3] === 'seed') await root.configure({instructions: '不支持改期、删除。'}, ctx);
  console.log(JSON.stringify((await root.agent(ctx)).instructions));
} finally { await harness.close(ctx); }
