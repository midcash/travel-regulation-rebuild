// Execute the actual renderer with a small DOM stand-in, no browser/network/package dependency.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

class Element {
  constructor() { this.children = []; this.value = ''; this.hidden = false; this.classList = {toggle() {}}; }
  set textContent(value) { this.ownText = String(value); this.children = []; }
  get textContent() { return (this.ownText || '') + this.children.map(c => c.textContent).join(' '); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.ownText = ''; this.children = children; }
  addEventListener() {}
}
const elements = new Map();
const document = {createElement: () => new Element(), querySelectorAll: () => [],
  getElementById(id) { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); }};
const context = vm.createContext({document, fetch: () => new Promise(() => {}), EventSource: class {}, assert});
vm.runInContext(fs.readFileSync(new URL('../../travel_agent/web/app.js', import.meta.url), 'utf8'), context);
const cases = [
  ['invalid_query', '查询参数需要补充或修正'], ['tool_error', '本地工具执行失败'],
  ['configuration_error', '查询配置错误'], ['supplier_error', '供应商查询失败'],
];
for (const [kind, label] of cases) {
  context.result = {status: 'error', failure_kind: kind, data: null, error: {type: 'SyntheticError'}};
  vm.runInContext('var container = document.createElement("div"); renderResults([result], container);', context);
  const rendered = context.container.textContent;
  assert.ok(rendered.includes(label), rendered);
  if (kind !== 'supplier_error') {
    assert.ok(!rendered.includes('供应商查询失败'));
    assert.ok(!rendered.includes('途牛'));
    assert.ok(!rendered.includes('尝试查询于'));
    assert.ok(!rendered.includes('结果取得于'));
  }
}
const attempt = '2026-10-19T10:00:00+08:00';
const observed = '2026-10-19T10:00:03+08:00';
context.result = {status: 'error', failure_kind: 'supplier_error', data: null,
  source: '途牛航班接口', attempted_at: attempt, error: {type: 'SupplierHTTPError'}};
vm.runInContext('container = document.createElement("div"); renderResults([result], container);', context);
assert.ok(context.container.textContent.includes('尝试查询于'));
assert.ok(!context.container.textContent.includes('结果取得于'));
context.result = {status: 'ok', source: '途牛航班接口', attempted_at: attempt, observed_at: observed,
  data: {origin: '杭州', destination: '三亚', departure_date: '2026-11-20', candidates: []}};
vm.runInContext('container = document.createElement("div"); renderResults([result], container);', context);
assert.ok(context.container.textContent.includes('结果取得于'));
assert.ok(context.container.textContent.includes('未返回候选'));
assert.ok(!context.container.textContent.includes('供应商查询失败'));
for (const [status, stage] of [...cases, ['empty', '本次查询无候选'], ['model_error', '模型答复失败'], ['worker_error', '会话进程异常']]) {
  context.state = {session_id: 'offline', messages: [], run: {status, stage, results: []}};
  vm.runInContext('render(state)', context);
  assert.equal(document.getElementById('status').textContent, stage);
}
console.log('render checks passed');
