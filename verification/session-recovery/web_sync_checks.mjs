// Unit checks for the actual renderer's async ordering; not browser acceptance.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

class Element {
  constructor() {
    this.children = []; this.value = ''; this.listeners = {};
    this.classList = {toggle() {}};
  }
  set textContent(value) { this.ownText = String(value); this.children = []; }
  get textContent() { return (this.ownText || '') + this.children.map(c => c.textContent).join(' '); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.ownText = ''; this.children = children; }
  addEventListener(name, handler) { this.listeners[name] = handler; }
  focus() {}
}

const elements = new Map();
const document = {createElement: () => new Element(), querySelectorAll: () => [],
  getElementById(id) { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); }};
let resolveInitial;
const initial = new Promise(resolve => { resolveInitial = resolve; });
let events;
let resetState;
const response = state => ({ok: true, json: async () => state});
const context = vm.createContext({document,
  fetch: url => url === '/api/session/reset' ? Promise.resolve(response(resetState)) : initial,
  EventSource: class { constructor() { events = this; } }});
vm.runInContext(fs.readFileSync(new URL('../../travel_agent/web/app.js', import.meta.url), 'utf8'), context);
const state = (id, revision, text) => ({session_id: id, revision,
  messages: [{id: 'message-' + revision, role: 'user', text}],
  run: {id: 'run-' + revision, status: 'complete', stage: text, results: []}, context_lost: false});
const event = value => events.onmessage({data: JSON.stringify(value)});
const title = () => document.getElementById('chat-title').textContent;
const name = process.argv[2];

if (name === 'stale-sse') {
  event(state('same-session', 12, 'newer'));
  event(state('same-session', 11, 'older'));
  assert.equal(title(), 'newer', 'An older SSE snapshot must not roll the page back');
} else if (name === 'late-fetch') {
  event(state('same-session', 12, 'newer'));
  resolveInitial(response(state('same-session', 11, 'older')));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(title(), 'newer', 'A delayed initial GET must not overwrite newer SSE state');
} else if (name === 'retired-session') {
  event(state('old-session', 20, 'old'));
  resetState = state('new-session', 0, 'new');
  resetState.messages = [];
  await elements.get('new-chat').listeners.click();
  assert.equal(title(), '新的出行想法');
  event(state('old-session', 21, 'retired'));
  assert.equal(title(), '新的出行想法', 'A previous session snapshot must not undo an explicit reset');
} else {
  throw new Error('Unknown synchronization test: ' + name);
}
console.log(name + ' passed');
