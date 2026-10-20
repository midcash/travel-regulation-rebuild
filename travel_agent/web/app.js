const $ = id => document.getElementById(id);
const busyStates = new Set(['running', 'cancelling']);
let current = null, submitting = false, lastKey = '';
const text = (tag, value, cls) => { const el = document.createElement(tag); el.textContent = value ?? ''; if (cls) el.className = cls; return el; };
const busy = () => submitting || busyStates.has(current?.run?.status);

function flightCard(candidate) {
  const card = text('article', '', 'flight-card');
  const top = text('div', '', 'flight-top');
  top.append(text('span', `${candidate.operator || '航司未提供'} · ${candidate.flight_no || '航班号未知'}`, 'flight-no'), text('span', candidate.meets_request ? '符合本次查询条件' : '请留意时刻或条件限制'));
  const body = text('div', '', 'flight-body');
  for (const side of ['departure', 'arrival']) {
    const block = text('div', '', 'flight-endpoint');
    const time = candidate[`${side}_at`];
    block.append(text('div', time ? time.slice(11, 16) : '时间未知', 'flight-time'), text('div', candidate[`${side}_airport`] || '机场未提供', 'airport'));
    body.append(block);
    if (side === 'departure') body.append(text('span', '', 'flight-line'));
  }
  const price = text('div', candidate.price_cny == null ? '价格未知' : `¥${candidate.price_cny}`, 'price');
  price.append(text('small', candidate.price_cny == null ? '供应商未提供完整报价' : '本次查询报价'));
  body.append(price);
  card.append(top, body, text('div', `${candidate.departure_at || '出发时间未知'} → ${candidate.arrival_at || '到达时间未知'}\n证据：${candidate.evidence_ref || '未提供'}`, 'flight-foot'));
  return card;
}

function renderResults(results, parent) {
  if (!results?.length) return;
  const container = text('div', '', 'flight-results');
  results.forEach(result => {
    const query = result.data;
    const caption = query ? `${query.origin} → ${query.destination} · ${query.departure_date}` : '航班查询';
    const meta = [caption];
    if (result.source) meta.push(result.source);
    if (result.attempted_at) meta.push(`尝试查询于 ${result.attempted_at.slice(0, 16).replace('T', ' ')}`);
    if (result.observed_at) meta.push(`结果取得于 ${result.observed_at.slice(0, 16).replace('T', ' ')}`);
    container.append(text('div', meta.join(' · '), 'query-meta'));
    if (result.status === 'error') {
      const labels = {invalid_query: '查询参数需要补充或修正', tool_error: '本地工具执行失败',
        configuration_error: '查询配置错误', supplier_error: '供应商查询失败'};
      container.append(text('div', `${labels[result.failure_kind] || '工具执行失败'} · ${result.error?.type || 'UnknownError'}。${result.error?.message || '未取得有效结果。'}`, 'result-notice'));
    }
    else if (!query?.candidates?.length) container.append(text('div', '本次查询成功，但未返回候选；这不代表全市场无航班。', 'result-notice'));
    else query.candidates.forEach(candidate => container.append(flightCard(candidate)));
  });
  parent.append(container);
}

function render(state) {
  const changedSession = current && current.session_id !== state.session_id;
  current = state;
  const hasMessages = state.messages.length > 0;
  $('content').classList.toggle('has-messages', hasMessages);
  $('chat-title').textContent = state.messages[0]?.text || '新的出行想法';
  const key = JSON.stringify([state.messages, state.run?.results, busyStates.has(state.run?.status)]);
  if (key !== lastKey || changedSession) {
    lastKey = key;
    const messages = $('messages');
    messages.replaceChildren();
    state.messages.forEach(message => {
      const row = text('article', '', `message ${message.role}`);
      if (message.role === 'assistant') row.append(text('span', '', 'orb assistant-icon'));
      const body = text('div', '', 'message-body');
      if (message.role === 'assistant') body.append(text('div', '行旅', 'message-label'));
      body.append(text('div', message.text, 'message-text'));
      renderResults(message.results, body);
      row.append(body); messages.append(row);
    });
    if (busyStates.has(state.run?.status)) {
      const live = text('div', '', 'flight-results');
      renderResults(state.run.results, live); messages.append(live);
    }
    if (hasMessages) messages.scrollTop = messages.scrollHeight;
  }
  const isBusy = busy();
  $('send').disabled = isBusy || !$('prompt').value.trim();
  $('new-chat').disabled = isBusy;
  $('mobile-new').disabled = isBusy;
  $('cancel').hidden = !busyStates.has(state.run?.status);
  $('cancel').disabled = state.run?.status === 'cancelling';
  $('status').textContent = state.run?.stage || '准备好，随时出发';
  if (state.context_lost) showError('会话上下文已丢失。请点击“开始新会话”；已有结果仍可查看。');
}

function showError(message) { $('error').textContent = message; $('error').hidden = !message; }
async function post(url, body) {
  const response = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body || {})});
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '请求未被接受，请检查输入。');
  return data;
}
async function refresh() { const response = await fetch('/api/session'); if (!response.ok) throw new Error('无法读取会话。'); render(await response.json()); }
$('composer').addEventListener('submit', async event => {
  event.preventDefault();
  const value = $('prompt').value.trim();
  if (!value || busy()) return;
  submitting = true; $('send').disabled = true; showError('');
  try { await post('/api/messages', {text: value}); $('prompt').value = ''; await refresh(); }
  catch (error) { showError(error.message); }
  finally { submitting = false; if (current) render(current); }
});
$('prompt').addEventListener('input', () => { $('send').disabled = busy() || !$('prompt').value.trim(); });
$('prompt').addEventListener('keydown', event => { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); $('composer').requestSubmit(); } });
$('cancel').addEventListener('click', async () => { try { await post('/api/cancel'); await refresh(); } catch (error) { showError(error.message); } });
$('new-chat').addEventListener('click', async () => { if (busy()) return; try { render(await post('/api/session/reset')); showError(''); $('prompt').focus(); } catch (error) { showError(error.message); } });
$('mobile-new').addEventListener('click', () => $('new-chat').click());
document.querySelectorAll('[data-example]').forEach(button => button.addEventListener('click', () => { if (!busy()) { $('prompt').value = button.dataset.example; $('prompt').dispatchEvent(new Event('input')); $('prompt').focus(); } }));
const events = new EventSource('/api/events');
events.onmessage = event => { $('connection').textContent = ''; render(JSON.parse(event.data)); };
events.onerror = () => { $('connection').textContent = '连接中断，正在重连'; };
refresh().catch(error => showError(error.message));
