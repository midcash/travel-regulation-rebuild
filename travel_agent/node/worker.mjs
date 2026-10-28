// Product worker: Pi owns the loop and conversation; Python owns supplier calls.
import readline from 'node:readline';
import * as sdk from '@earendil-works/pi-coding-agent';
import * as ai from '@earendil-works/pi-ai';

const emit = value => process.stdout.write(JSON.stringify(value) + '\n');
const pending = new Map();
let manager;
const entries = () => manager.getEntries();
let session, runtime, faux, commandId, busy = false, cancelled = false, modelCalls = 0;

function requestTool(id, name, args, signal) {
  return new Promise((resolve, reject) => {
    const abort = () => { pending.delete(id); reject(new Error('Tool cancelled')); };
    if (signal?.aborted) return abort();
    pending.set(id, value => { signal?.removeEventListener('abort', abort); resolve(value); });
    signal?.addEventListener('abort', abort, {once: true});
    emit({type: 'tool_request', id, name, args, commandId, entries: entries()});
  });
}

async function initialize(c) {
  runtime = await sdk.ModelRuntime.create({credentials: new ai.InMemoryCredentialStore(),
    modelsStore: new ai.InMemoryModelsStore(), modelsPath: null, refreshOnCreate: false});
  if (c.fake) {
    faux = ai.fauxProvider({provider: 'travel-offline', models: [{id: 'synthetic', reasoning: false}], responses: []});
    runtime.registerNativeProvider(faux.provider);
    await runtime.setRuntimeApiKey('travel-offline', 'offline');
  } else {
    if (!process.env.DEEPSEEK_API_KEY) throw new Error('Missing model configuration');
    runtime.registerProvider('deepseek', {baseUrl: process.env.DEEPSEEK_BASE_URL || 'https://api.deepseek.com',
      api: 'openai-completions', models: [{id: c.model, name: c.model, input: ['text'], reasoning: true,
        contextWindow: 131072, maxTokens: 8192,
        // SDK metadata only. The product never treats this as a billing estimate.
        cost: {input: 0, output: 0, cacheRead: 0, cacheWrite: 0}}]});
    await runtime.setRuntimeApiKey('deepseek', process.env.DEEPSEEK_API_KEY);
  }
  const model = faux?.getModel() || runtime.getModel('deepseek', c.model);
  const stream = runtime.streamSimple.bind(runtime);
  runtime.streamSimple = (m, context, options) => {
    if (cancelled || ++modelCalls > 4) throw new Error('Run stopped or model call limit exceeded');
    emit({type: 'model_start', commandId, call: modelCalls});
    const output = ai.createAssistantMessageEventStream();
    void (async () => {
      const id = 'checkpoint-' + crypto.randomUUID();
      await new Promise(resolve => {
        pending.set(id, resolve);
        emit({type: 'checkpoint', id, entries: entries(), commandId});
      });
      if (cancelled) throw new Error('Run stopped');
      const source = stream(m, context, {...options, maxTokens: Math.min(options?.maxTokens ?? 2048, 2048)});
      for await (const event of source) output.push(event);
      output.end(await source.result());
    })().catch(error => {
      const failure = {...ai.fauxAssistantMessage('', {stopReason: 'error', errorMessage: String(error.message)}),
        model: m.id, provider: m.provider, api: m.api};
      output.push({type: 'error', reason: 'error', error: failure}); output.end(failure);
    });
    return output;
  };
  const resourceLoader = {
    getExtensions: () => ({extensions: [], errors: [], runtime: sdk.createExtensionRuntime()}),
    getSkills: () => ({skills: [], diagnostics: []}), getPrompts: () => ({prompts: [], diagnostics: []}),
    getThemes: () => ({themes: [], diagnostics: []}), getAgentsFiles: () => ({agentsFiles: []}),
    getSystemPrompt: () => c.system, getSystemPromptSource: () => undefined,
    getAppendSystemPrompt: () => [], getAppendSystemPromptSources: () => [], extendResources() {}, async reload() {},
  };
  const customTools = c.tools.map(t => ({...t, label: t.name,
    execute: async (id, args, signal) => ({content: [{type: 'text', text: JSON.stringify(await requestTool(id, t.name, args, signal))}], details: {}}),
  }));
  manager = sdk.SessionManager.inMemory(process.cwd(), undefined, c.entries);
  ({session} = await sdk.createAgentSession({cwd: process.cwd(), agentDir: process.env.PI_CODING_AGENT_DIR,
    model, modelRuntime: runtime, thinkingLevel: 'off', resourceLoader, customTools,
    tools: customTools.map(t => t.name), sessionManager: manager,
    settingsManager: sdk.SettingsManager.inMemory({compaction: {enabled: false},
      retry: {enabled: false, provider: {maxRetries: 0}}, cacheWarming: 'off', defaultTools: []})}));
  session.subscribe(e => {
    if (e.type === 'tool_execution_end') emit({type: 'tool_end', commandId});
    if (e.type === 'message_end' && e.message.role === 'assistant') {
      const m = e.message;
      emit({type: 'message_end', commandId, stopReason: m.stopReason, usage: m.usage, error: m.errorMessage,
        text: m.content.filter(b => b.type === 'text').map(b => b.text).join('')});
    }
  });
  return {model: model.id, toolNames: session.getActiveToolNames(), sessionFile: session.sessionFile};
}

async function handle(c) {
  if (c.op === 'storage_failed') { cancelled = true; await session?.abort(); return; }
  if (c.op === 'checkpoint_ack') { const resolve = pending.get(c.id); pending.delete(c.id); resolve?.(); return; }
  if (c.op === 'tool_result') { const resolve = pending.get(c.id); pending.delete(c.id); resolve?.(c.result); return; }
  try {
    let result;
    if (c.op === 'init') result = await initialize(c);
    else if (c.op === 'prompt') {
      if (busy) throw new Error('Session busy');
      busy = true; cancelled = false; modelCalls = 0; commandId = c.id;
      try {
        if (faux) faux.setResponses((c.responses || []).map(r => ai.fauxAssistantMessage(
          r.tool ? ai.fauxToolCall(r.tool, r.args) : (r.text || ''),
          {stopReason: r.error ? 'error' : (r.tool ? 'toolUse' : 'stop'), errorMessage: r.error})));
        await session.prompt(c.text); await session.waitForIdle();
        result = {modelCalls, entries: entries()};
      } finally { busy = false; }
    } else if (c.op === 'cancel') {
      cancelled = true;
      for (const [id, resolve] of pending) if (id.startsWith('checkpoint-')) { pending.delete(id); resolve(); }
      await session?.abort(); result = {cancelled: true};
    }
    else if (c.op === 'shutdown') {
      cancelled = true; await session?.abort(); session?.dispose();
      emit({type: 'done', id: c.id, result: {closed: true}}); process.exit(0);
    } else throw new Error('Unknown operation');
    emit({type: 'done', id: c.id, result});
  } catch (error) { emit({type: 'failed', id: c.id, error: String(error.message)}); }
}
const input = readline.createInterface({input: process.stdin});
input.on('line', line => { try { void handle(JSON.parse(line)); } catch { emit({type: 'protocol_error'}); } });
input.on('close', () => { void (async () => { await session?.abort(); session?.dispose(); process.exit(0); })(); });
emit({type: 'ready'});
