const $ = id => document.getElementById(id);
const displayZone = 'Etc/GMT-8'; // IANA's Etc/GMT sign is reversed: this is fixed UTC+8.
const zoneLabel = 'UTC+8';
const formatTime = value => new Intl.DateTimeFormat('zh-CN', {timeZone: displayZone, dateStyle: 'medium', timeStyle: 'short', hourCycle: 'h23'}).format(new Date(value));
const formatClock = value => new Intl.DateTimeFormat('zh-CN', {timeZone: displayZone, timeStyle: 'short', hourCycle: 'h23'}).format(new Date(value));
let calendarFailed = false;
const calendar = new FullCalendar.Calendar($('calendar'), {
  initialView: 'dayGridMonth', locale: 'zh-cn', timeZone: displayZone, height: 'auto',
  buttonText: {today: '今天', month: '月', week: '周', day: '日'},
  headerToolbar: {left: 'prev,next today', center: 'title', right: 'dayGridMonth,timeGridWeek,timeGridDay'},
  events: '/calendar',
  eventSourceFailure: () => { calendarFailed = true; $('calendar-error').textContent = '日历加载失败，正在重试。'; },
  eventSourceSuccess: () => { calendarFailed = false; $('calendar-error').textContent = ''; },
  eventClick: ({event}) => { $('calendar-detail').textContent = `${event.title} · ${formatTime(event.start)} — ${formatTime(event.end)}（${zoneLabel}）`; },
});
calendar.render();
// FullCalendar's inner tables cache pixel widths; update after the container resizes.
let calendarWidth = 0;
new ResizeObserver(([entry]) => {
  if (entry.contentRect.width === calendarWidth) return;
  calendarWidth = entry.contentRect.width;
  requestAnimationFrame(() => calendar.updateSize());
}).observe($('calendar'));
$('calendar-zone').textContent = `显示时区：${zoneLabel}（固定 +08:00）`;
// Layout-only controls: navigation and the collapsible schedule rail never touch conversation state.
const app = $('app');
const scheduleButtons = [$('schedule-toggle'), $('schedule-toggle-notes')];
let scheduleOpen = !window.matchMedia('(max-width: 860px)').matches;
function setSchedule(open) {
  scheduleOpen = open;
  app.classList.toggle('schedule-open', open);
  app.classList.toggle('schedule-closed', !open);
  for (const button of scheduleButtons) {
    button.setAttribute('aria-expanded', String(open));
    button.textContent = open ? '收起日程' : '显示日程';
  }
  requestAnimationFrame(() => calendar.updateSize());
}
for (const button of scheduleButtons) button.onclick = () => setSchedule(!scheduleOpen);
$('schedule-close').onclick = () => setSchedule(false);
setSchedule(scheduleOpen);
let pendingAutoScroll = false;
let narrowLayout = window.matchMedia('(max-width: 860px)').matches;
addEventListener('resize', () => {
  const nowNarrow = window.matchMedia('(max-width: 860px)').matches;
  if (nowNarrow === narrowLayout) return;
  narrowLayout = nowNarrow;
  setSchedule(!nowNarrow);
});
const composerText = $('text');
composerText.addEventListener('input', () => {
  composerText.style.height = 'auto';
  composerText.style.height = Math.min(composerText.scrollHeight, 180) + 'px';
});
function showView() {
  const notes = location.hash === '#/notes';
  $('chat-view').hidden = notes;
  $('notes-view').hidden = !notes;
  for (const [id, active] of [['nav-chat', !notes], ['nav-notes', notes]]) {
    $(id).classList.toggle('active', active);
    if (active) $(id).setAttribute('aria-current', 'page'); else $(id).removeAttribute('aria-current');
  }
  document.title = (notes ? '笔记与数据' : '对话') + ' · Pi Durable';
  if (!notes && pendingAutoScroll) requestAnimationFrame(() => {
    const scroller = $('chat-scroll');
    scroller.scrollTop = scroller.scrollHeight;
    pendingAutoScroll = false;
  });
}
addEventListener('hashchange', showView);
showView();
let renderedCalendar = '', renderedPending = '';
let sending = false, healthy = false, current;
let refreshId = 0;
let connectionError = false;
let pending;
try { pending = JSON.parse(sessionStorage.getItem('durable-mvp-pending') || 'null'); } catch { pending = null; }
async function api(url, body) {
  const response = await fetch(url, {method: body === undefined ? 'GET' : 'POST',
    headers: body === undefined ? {} : {'Content-Type': 'application/json'},
    body: body === undefined ? undefined : JSON.stringify(body), signal: AbortSignal.timeout(15000)});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
  return result;
}
function controls() {
  $('send').disabled = !healthy || sending || current?.busy || !!pending;
  $('cancel').disabled = !healthy || !current?.busy;
  $('timer').disabled = !healthy || sending;
  $('retry').hidden = !pending;
  $('retry').disabled = !healthy || sending;
  for (const button of $('calendar-confirmations').querySelectorAll('button')) button.disabled = !healthy || sending || current?.busy || !!pending;
  const items = Object.values(current?.calendar.pending ?? {}).filter(item => !item.result);
  const action = items.some(item => item.updateId) ? (items.some(item => !item.updateId) ? '创建或修改' : '修改') : '创建';
  setText('confirmation-status', !healthy ? '连接中断，恢复后可确认。' : pending ? '有未确认提交的请求，请先重试同一请求。' : sending || current?.busy ? '正在处理会话，完成后可确认。' : `请核对时间，再选择是否确认${action}。`);
}
$('confirmation-shortcut').onclick = () => {
  if (!scheduleOpen) setSchedule(true);
  requestAnimationFrame(() => {
    const panel = $('confirmation-panel');
    panel.scrollIntoView({block: 'center'});
    panel.focus({preventScroll: true});
  });
};
let renderedMessages = '';
function setText(id, value) { if ($(id).textContent !== value) $(id).textContent = value; }
function draw(state) {
  current = state;
  setText('mode', state.mode === 'offline' ? '离线演示 · 无模型费用' : `真实模型 · ${state.model.modelId}`);
  setText('description', state.mode === 'offline' ? '当前使用脚本化回显验证运行链路，不具备真实语言理解能力。会话、笔记和后台任务保存在本机。' : '由 pi-ai 接入 DeepSeek。提交消息和恢复未完成任务可能产生模型费用。');
  setText('status', state.busy ? '正在执行，可取消或刷新' : '就绪 · 已载入保存状态');
  // The conversation is a live region: redraw only when its content really changed.
  const signature = JSON.stringify(state.messages);
  if (signature !== renderedMessages) {
    const scroller = $('chat-scroll');
    const follow = renderedMessages === '' || scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 140;
    renderedMessages = signature;
    $('messages').replaceChildren();
    for (const message of state.messages) {
      if (!message.text && !message.calls.length) continue;
      const item = document.createElement('article');
      item.className = 'message ' + message.role;
      const label = document.createElement('strong');
      label.textContent = {user: '你', assistant: '助手', toolResult: '工具返回'}[message.role];
      if (message.role !== 'toolResult') label.className = 'sr-only';
      item.append(label);
      const text = document.createElement('p'); text.textContent = message.text;
      item.append(text);
      if (message.calls.length) { const calls = document.createElement('small'); calls.textContent = '调用工具：' + message.calls.join(', '); item.append(calls); }
      $('messages').append(item);
    }
    if (follow) {
      // A hidden chat view has no layout, so defer the scroll until it becomes visible.
      if ($('chat-view').hidden) pendingAutoScroll = true;
      else { scroller.scrollTop = scroller.scrollHeight; pendingAutoScroll = false; }
    }
  }
  setText('note', state.note || '暂无笔记');
  const calendarSignature = JSON.stringify(state.calendar.events);
  if (calendarSignature !== renderedCalendar) { renderedCalendar = calendarSignature; calendar.refetchEvents(); }
  const pendingSignature = JSON.stringify(state.calendar.pending);
  if (pendingSignature !== renderedPending) {
    renderedPending = pendingSignature;
    $('calendar-confirmations').replaceChildren();
    const unresolved = Object.entries(state.calendar.pending).filter(([, item]) => !item.result);
    $('confirmation-panel').hidden = unresolved.length === 0;
    $('confirmation-shortcut').hidden = unresolved.length === 0;
    setText('confirmation-shortcut', `查看待确认日程（${unresolved.length}）`);
    for (const [id, item] of unresolved) {
      const card = document.createElement('article');
      const title = document.createElement('h4'); title.textContent = item.event.title;
      const detail = document.createElement('p');
      detail.textContent = `${formatTime(item.event.start)} — ${formatTime(item.event.end)}（${zoneLabel}）`;
      const effect = document.createElement('p');
      effect.textContent = item.updateId ? '尚未修改。确认后将替换原日程，保留其他已有日程。' : '尚未创建。确认后新增日程，保留已有日程。';
      const conflicts = document.createElement('p');
      conflicts.textContent = '重叠安排：' + (item.conflicts ?? []).map(e => `${e.title}（${formatTime(e.start)} — ${formatTime(e.end)}）`).join('；');
      const button = document.createElement('button'); button.textContent = item.updateId ? '确认修改' : '确认创建';
      button.onclick = () => {
        if (sending || pending || !healthy || current?.busy) return;
        pending = {text: '确认重叠 ' + id, requestId: crypto.randomUUID()};
        sessionStorage.setItem('durable-mvp-pending', JSON.stringify(pending)); void submit();
      };
      card.append(title, detail, effect, conflicts, button); $('calendar-confirmations').append(card);
    }
  }
  setText('identity', `会话 ${state.conversationId} · ${state.model.provider}`);
  setText('counts', `本进程模型调用 ${state.counts.model} · 工具调用 ${state.counts.tool}`);
  $('timers').replaceChildren(...state.timers.map(timer => {
    const li = document.createElement('li');
    li.textContent = timer.state?.status === 'terminal' ? '计时完成' : '等待到期 · ' + formatClock(timer.until);
    return li;
  }));
  // Page-delivery channel for reminders: render scheduled/delivered reminders and
  // toast once when a reminder transitions to 'delivered'. The committed state
  // is the source of truth, so even if a system notification failed the user
  // still sees the reminder here.
  const reminders = state.reminders ?? [];
  if ($('reminders')) {
    $('reminders').replaceChildren(...reminders.map(r => {
      const li = document.createElement('li');
      const when = formatTime(r.start);
      li.textContent = (r.status === 'delivered' ? '已提醒 · ' : '待提醒 · ') + (r.title || '日程') + ' · ' + when;
      return li;
    }));
  }
  for (const r of reminders) {
    const key = r.eventId + '@' + (r.remindAt ?? '');
    if (r.status === 'delivered' && !shownReminders.has(key)) {
      shownReminders.add(key);
      showReminderToast(r);
    }
  }
}
const shownReminders = new Set();
function showReminderToast(r) {
  const host = $('reminder-toast');
  if (!host) return;
  const card = document.createElement('div');
  card.className = 'reminder-toast-item';
  card.setAttribute('role', 'status');
  card.textContent = '日程提醒：' + (r.title || '日程') + ' 将于 ' + formatTime(r.start) + ' 开始';
  const close = document.createElement('button');
  close.textContent = '知道了';
  close.onclick = () => card.remove();
  card.append(close);
  host.append(card);
  // Best-effort system notification (browser); failure is silent and the page
  // channel above already delivered. Never blocks the page channel.
  try {
    if ('Notification' in window) {
      if (Notification.permission === 'granted') new Notification('日程提醒', {body: card.textContent});
      else if (Notification.permission !== 'denied') Notification.requestPermission();
    }
  } catch (_) { /* system channel is best-effort; page channel already shown */ }
}
async function refresh() {
  const id = ++refreshId;
  try {
    const state = await api('/state'); if (id !== refreshId) return;
    healthy = true; draw(state);
    if (calendarFailed) { calendarFailed = false; calendar.refetchEvents(); }
    if (connectionError) { $('error').textContent = ''; connectionError = false; calendar.refetchEvents(); }
  } catch (error) {
    if (id !== refreshId) return;
    healthy = false; connectionError = true;
    $('status').textContent = '连接中断，等待服务恢复'; $('error').textContent = error.message;
  }
  controls();
}
async function submit() {
  sending = true; controls(); $('error').textContent = '';
  try {
    await api('/prompt', pending);
    if ($('text').value === pending.text) { $('text').value = ''; composerText.style.height = ''; }
    pending = null; sessionStorage.removeItem('durable-mvp-pending');
  } catch (error) { $('error').textContent = error.message + '。请求未确认，可用相同 ID 重试。'; }
  finally { sending = false; await refresh(); }
}
$('composer').addEventListener('submit', event => {
  event.preventDefault();
  if (sending || pending || !healthy || current?.busy || !$('text').value.trim()) return;
  pending = {text: $('text').value, requestId: crypto.randomUUID()};
  sessionStorage.setItem('durable-mvp-pending', JSON.stringify(pending));
  void submit();
});
$('retry').onclick = () => { if (pending) void submit(); };
for (const [id, endpoint] of [['cancel', '/abort'], ['timer', '/timer']]) $(id).onclick = async () => {
  sending = true; controls(); $('error').textContent = '';
  try { await api(endpoint, {}); } catch (error) { $('error').textContent = error.message; }
  finally { sending = false; await refresh(); }
};
async function poll() { await refresh(); setTimeout(poll, 700); }
void poll();
