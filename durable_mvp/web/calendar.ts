import {element, setText} from './dom.js';
import type {State} from './api.js';

declare const FullCalendar: typeof import('fullcalendar');

const displayZone = 'Etc/GMT-8'; // IANA's Etc/GMT sign is reversed: this is fixed UTC+8.
const zoneLabel = 'UTC+8';
const formatter = new Intl.DateTimeFormat('zh-CN', {
  timeZone: displayZone, dateStyle: 'medium', timeStyle: 'short', hourCycle: 'h23',
});

export function setupCalendar(onConfirm: (id: string) => void) {
  const container = element('calendar');
  let failed = false;
  let renderedEvents = '';
  let renderedPending = '';
  let width = 0;
  const calendar = new FullCalendar.Calendar(container, {
    initialView: 'dayGridMonth', locale: 'zh-cn', timeZone: displayZone, height: 'auto',
    buttonText: {today: '今天', month: '月', week: '周', day: '日'},
    headerToolbar: {left: 'prev,next today', center: 'title', right: 'dayGridMonth,timeGridWeek,timeGridDay'},
    events: '/calendar',
    eventSourceFailure: () => {
      failed = true;
      setText('calendar-error', '日历加载失败，正在重试。');
    },
    eventSourceSuccess: () => {
      failed = false;
      setText('calendar-error', '');
    },
    eventClick: ({event}) => {
      if (event.start && event.end) setText('calendar-detail', `${event.title} · ${formatRange(event.start, event.end)}（${zoneLabel}）`);
    },
  });

  function resize() {
    calendar.updateSize();
  }

  calendar.render();
  // FullCalendar caches table widths between renders.
  new ResizeObserver(([entry]) => {
    if (!entry || entry.contentRect.width === width) return;
    width = entry.contentRect.width;
    requestAnimationFrame(resize);
  }).observe(container);
  setText('calendar-zone', `显示时区：${zoneLabel}（固定 +08:00）`);

  function render(state: State['calendar']) {
    const events = JSON.stringify(state.events);
    if (events !== renderedEvents) {
      renderedEvents = events;
      calendar.refetchEvents();
    }
    const pending = JSON.stringify(state.pending);
    if (pending === renderedPending) return;
    renderedPending = pending;
    const confirmations = element('calendar-confirmations');
    const unresolved = Object.entries(state.pending).filter(([, item]) => !item.result);
    confirmations.replaceChildren();
    element('confirmation-panel').hidden = unresolved.length === 0;
    element('confirmation-shortcut').hidden = unresolved.length === 0;
    setText('confirmation-shortcut', `查看待确认日程（${unresolved.length}）`);
    for (const [id, item] of unresolved) {
      const card = document.createElement('article');
      const title = document.createElement('h4');
      title.textContent = item.event.title;
      const detail = document.createElement('p');
      detail.textContent = `${formatRange(item.event.start, item.event.end)}（${zoneLabel}）`;
      const effect = document.createElement('p');
      effect.textContent = item.updateId
        ? '尚未修改。确认后将替换原日程，保留其他已有日程。'
        : '尚未创建。确认后新增日程，保留已有日程。';
      const conflicts = document.createElement('p');
      conflicts.textContent = '重叠安排：' + (item.conflicts ?? []).map(event =>
        `${event.title}（${formatRange(event.start, event.end)}）`).join('；');
      const button = document.createElement('button');
      button.textContent = item.updateId ? '确认修改' : '确认创建';
      button.onclick = () => onConfirm(id);
      card.append(title, detail, effect, conflicts, button);
      confirmations.append(card);
    }
  }

  function setConfirmationControls(disabled: boolean, status?: string, pending?: State['calendar']['pending']) {
    for (const button of element('calendar-confirmations').querySelectorAll('button')) button.disabled = disabled;
    const unresolved = Object.values(pending ?? {}).filter(item => !item.result);
    const creates = unresolved.some(item => !item.updateId);
    const updates = unresolved.some(item => item.updateId);
    const action = updates ? (creates ? '创建或修改' : '修改') : '创建';
    setText('confirmation-status', status ?? `请核对时间，再选择是否确认${action}。`);
  }

  return {
    resize, render, setConfirmationControls,
    retryIfFailed() {
      if (!failed) return;
      failed = false;
      calendar.refetchEvents();
    },
    refetch: () => calendar.refetchEvents(),
  };
}

function formatRange(start: string | Date, end: string | Date): string {
  return `${formatter.format(new Date(start))} — ${formatter.format(new Date(end))}`;
}
